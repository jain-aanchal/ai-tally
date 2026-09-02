# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :class:`gateway.edge_keys.EdgeKeyStore.changes_since` (Initiative 2 §6.2 review).

The delta feed pages api-key changes by a keyset watermark over ``(GREATEST(created_at, revoked_at),
id)``. Because those timestamps are stamped at STATEMENT time, not commit time, a slow transaction
can commit a row whose watermark sits below a cursor that a later-but-faster commit already advanced
past. Under plain keyset pagination that row is skipped forever. The store guards against this with a
safe-lag window: it never returns (and so never advances the cursor past) a row whose watermark is
newer than ``now() - edge_key_safe_lag_seconds``.

These tests drive the real store SQL through a fake psycopg cursor that models the api_keys table,
the DATABASE clock (``now()``), and per-row commit visibility, so the skip scenario is reproduced
deterministically without a live Postgres.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from types import SimpleNamespace

from gateway import edge_keys
from gateway.edge_keys import EdgeKeyStore

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _ts(seconds: int) -> dt.datetime:
    """A DB timestamp `seconds` after the epoch. Keeps the arithmetic obvious in the assertions."""
    return _EPOCH + dt.timedelta(seconds=seconds)


@dataclass
class Row:
    key_hash: str
    tenant_id: str
    scope: str
    created_at: dt.datetime
    revoked_at: dt.datetime | None
    row_id: str
    visible_at: dt.datetime  # models commit time: the row is invisible to reads before this

    def watermark(self) -> dt.datetime:
        return max(self.created_at, self.revoked_at or self.created_at)


class FakeCursor:
    """Emulates the store's single keyset query against an in-memory, clock-and-commit-aware table."""

    def __init__(self, table: "FakeTable") -> None:
        self._table = table
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple) -> None:
        after_wm, after_id, lag_seconds, limit = params
        boundary = self._table.now - dt.timedelta(seconds=float(lag_seconds))
        visible = [r for r in self._table.rows if r.visible_at <= self._table.now]
        matched = [
            r
            for r in visible
            if (r.watermark(), r.row_id) > (after_wm, after_id) and r.watermark() <= boundary
        ]
        matched.sort(key=lambda r: (r.watermark(), r.row_id))
        self._result = [
            (r.key_hash, r.tenant_id, r.scope, r.revoked_at, r.watermark(), r.row_id)
            for r in matched[:limit]
        ]

    def fetchall(self) -> list[tuple]:
        return self._result

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, table: "FakeTable") -> None:
        self._table = table

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._table)

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeTable:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.now = _EPOCH


def _store(monkeypatch, table: FakeTable, *, lag_seconds: float) -> EdgeKeyStore:
    monkeypatch.setattr(edge_keys.psycopg, "connect", lambda _dsn: FakeConn(table))
    settings = SimpleNamespace(
        postgres_dsn="postgresql://ignored", edge_key_safe_lag_seconds=lag_seconds
    )
    return EdgeKeyStore(settings)


def _build_out_of_commit_order_table() -> FakeTable:
    """Key B is created after key A but commits FIRST; A's slow transaction commits later.

    A: created at t=99, commits (visible) at t=104.  watermark 99.
    B: created at t=100, commits (visible) at t=100. watermark 100.
    """
    table = FakeTable()
    table.rows = [
        Row("hashA", "tenantA", "write", _ts(99), None, "keyA", visible_at=_ts(104)),
        Row("hashB", "tenantB", "write", _ts(100), None, "keyB", visible_at=_ts(100)),
    ]
    return table


def test_out_of_commit_order_row_is_not_skipped_with_safe_lag(monkeypatch) -> None:
    table = _build_out_of_commit_order_table()
    store = _store(monkeypatch, table, lag_seconds=5.0)

    # Poll 1 at t=101: B has committed (wm 100) but is still inside the 5s lag window (boundary 96),
    # so the feed holds it back and does NOT advance the cursor past it.
    table.now = _ts(101)
    changes, cursor = store.changes_since(None)
    assert changes == []
    assert cursor == ""  # unchanged: no row was safe to emit yet

    # Poll 2 at t=110: A's slow transaction has now committed (wm 99), and both rows are outside the
    # lag window (boundary 105). Both are delivered, ordered by watermark, so A is NOT skipped.
    table.now = _ts(110)
    changes, cursor = store.changes_since(cursor)
    assert [c.key_hash for c in changes] == ["hashA", "hashB"]


def test_without_safe_lag_the_late_commit_is_skipped(monkeypatch) -> None:
    # The regression itself: with NO safe-lag window (lag 0) the exact scenario strands key A.
    table = _build_out_of_commit_order_table()
    store = _store(monkeypatch, table, lag_seconds=0.0)

    # Poll 1 at t=101: only B is visible; it is emitted immediately and the cursor advances past it.
    table.now = _ts(101)
    changes, cursor = store.changes_since(None)
    assert [c.key_hash for c in changes] == ["hashB"]

    # Poll 2 at t=110: A has committed but its watermark (99) is below the cursor (100, keyB), so the
    # keyset predicate excludes it forever. This asserts the bug the safe-lag window fixes.
    table.now = _ts(110)
    changes, _ = store.changes_since(cursor)
    assert changes == []


def test_revocation_propagates_once_outside_the_lag_window(monkeypatch) -> None:
    table = FakeTable()
    table.rows = [Row("hashA", "tenantA", "write", _ts(10), None, "keyA", visible_at=_ts(10))]
    store = _store(monkeypatch, table, lag_seconds=5.0)

    table.now = _ts(30)
    changes, cursor = store.changes_since(None)
    assert [c.key_hash for c in changes] == ["hashA"]
    assert changes[0].revoked_at is None

    # Revoke at t=40 (watermark rises to 40). Visible immediately; picked up once past the lag window.
    table.rows[0].revoked_at = _ts(40)
    table.rows[0].visible_at = _ts(40)
    table.now = _ts(50)
    changes, _ = store.changes_since(cursor)
    assert len(changes) == 1
    assert changes[0].revoked_at == _ts(40)
