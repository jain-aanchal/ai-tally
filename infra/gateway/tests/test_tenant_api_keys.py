# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :class:`gateway.tenant_api_keys.TenantApiKeyStore.rotate` (Initiative 1 review).

Rotation mints a replacement key and revokes the old one in one transaction. The replacement must
inherit the ORIGINAL minter (``created_by``) when the caller does not supply one, so a rotation
never silently blanks the audit trail. These tests drive the store against a fake psycopg
connection so the exact SQL and the INSERT parameters are asserted without a live Postgres.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from gateway import tenant_api_keys
from gateway.tenant_api_keys import ApiKeyNotFoundError, TenantApiKeyStore

TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
KEY_ID = "11111111-1111-4111-8111-111111111111"
ORIGINAL_MINTER = "user_original_minter"


class FakeCursor:
    """Answers the resolver + rotate SQL from canned rows, recording every INSERT's parameters."""

    def __init__(self, *, old_row: tuple | None) -> None:
        self._old_row = old_row
        self._result: tuple | None = None
        self.insert_params: tuple | None = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT name, scope, created_by FROM api_keys"):
            self._result = self._old_row
        elif s.startswith("INSERT INTO api_keys"):
            self.insert_params = params
            # Echo an inserted row shaped like the RETURNING clause. created_by is params[5].
            self._result = (
                "22222222-2222-4222-8222-222222222222",
                params[3],  # name
                params[4],  # token_prefix
                params[2],  # scope
                params[5],  # created_by
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                None,
                None,
            )
        else:  # UPDATE ... revoked_at, and anything else
            self._result = None
            self.rowcount = 1

    def fetchone(self) -> tuple | None:
        return self._result

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, cur: FakeCursor) -> None:
        self._cur = cur
        self.committed = False

    def cursor(self) -> FakeCursor:
        return self._cur

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _store_with(monkeypatch: pytest.MonkeyPatch, cur: FakeCursor) -> TenantApiKeyStore:
    monkeypatch.setattr(tenant_api_keys.psycopg, "connect", lambda _dsn: FakeConn(cur))
    return TenantApiKeyStore(SimpleNamespace(postgres_dsn="postgresql://ignored"))


def test_rotate_carries_original_created_by_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    # Old key was minted by ORIGINAL_MINTER; the rotate call supplies no created_by.
    cur = FakeCursor(old_row=("prod key", "write", ORIGINAL_MINTER))
    store = _store_with(monkeypatch, cur)

    minted = store.rotate(TENANT_UUID, KEY_ID)

    # The INSERT must carry the original minter, not NULL.
    assert cur.insert_params is not None
    assert cur.insert_params[5] == ORIGINAL_MINTER
    assert minted.meta.created_by == ORIGINAL_MINTER


def test_rotate_prefers_explicit_created_by(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = FakeCursor(old_row=("prod key", "write", ORIGINAL_MINTER))
    store = _store_with(monkeypatch, cur)

    minted = store.rotate(TENANT_UUID, KEY_ID, created_by="user_rotator")

    # A supplied minter wins over the inherited one.
    assert cur.insert_params is not None
    assert cur.insert_params[5] == "user_rotator"
    assert minted.meta.created_by == "user_rotator"


def test_rotate_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = FakeCursor(old_row=None)
    store = _store_with(monkeypatch, cur)
    with pytest.raises(ApiKeyNotFoundError):
        store.rotate(TENANT_UUID, KEY_ID)
