# SPDX-License-Identifier: Apache-2.0
"""Per-tenant, per-integration ingest watermarks (CTO-219).

WHY this exists. :meth:`gateway.integration_workers.IngestWorker.run_cycle` had no cursor, so a
15-minute cadence asked Segment and HubSpot for the provider's whole default payload 96 times a day
and Pendo's 48 times. This module is the watermark that makes each cycle ask only for what is new.

WHAT THE COST ACTUALLY WAS, stated accurately. Re-sending the same events did not corrupt anything.
``BusinessEventId`` is the provider's own stable id and ``business_events`` is
``ReplacingMergeTree(IngestedAt) ORDER BY (TenantId, BusinessEventId)``, so re-insertion of an event
already present collapses to one row by design. The genuine costs are write amplification (every
cycle re-inserts every event in the window, and the background merge then throws the duplicates
away) and a transient pre-merge double count on the read paths that do not use ``FINAL``, which is
10 of the 12 ``business_events`` reads in ``web/lib/clickhouse.ts``. Both are worth fixing. Neither
is data loss, and this module should not be described as fixing one.

THE WINDOW. Each cycle asks for ``[since, until]`` where ``until`` is the moment the cycle started
and ``since`` is the stored cursor. Afterwards the cursor advances to ``until`` minus a
per-connector OVERLAP, never backwards. The overlap is the whole reason this is safe:

* A provider that has not yet aggregated an event when we ask for it would otherwise lose that event
  forever, because the next cycle would start after it. Pendo documents roughly five minutes of
  aggregation latency, which is precisely the number the 30-minute Pendo cadence in
  :mod:`gateway.worker_jobs` was chosen against, so Pendo's overlap is sized above it.
* Re-fetching the overlap costs nothing but bandwidth, because re-insertion is idempotent (above).

So the trade is deliberately asymmetric: a little duplicated fetch, never a dropped event.

A FIRST RUN with no cursor is floored at :data:`INITIAL_LOOKBACK_S`, not at "all history". A tenant
who connects Segment for the first time should get a bounded, useful initial window on a 15-minute
cadence, not an unbounded backfill that times out and then retries every 15 minutes forever. A real
historical backfill is a different operation with a different shape (see ``scripts/backfill_*.py``
for the connectors' equivalent) and it does not belong on a cadence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg

from gateway.config import Settings

logger = logging.getLogger("tally.gateway.ingest_cursors")

#: The connectors this table serves. Must stay in lockstep with the CHECK constraint in
#: ``0028_tenant_ingest_cursors.sql``.
ALLOWED_CURSOR_CONNECTORS: frozenset[str] = frozenset({"segment", "hubspot", "pendo"})

#: How far back a FIRST cycle reaches when no cursor exists. Seven days: wide enough that a tenant
#: who connects a source sees something immediately and that a gateway down for a long weekend
#: catches up in one cycle, narrow enough that the pull is one bounded request rather than a
#: backfill. Every subsequent cycle asks for roughly one cadence plus one overlap.
INITIAL_LOOKBACK_S = 7 * 24 * 3600.0

#: Per-connector overlap, in seconds: how far BEHIND the end of a cycle's window its cursor is left.
#: Sized to each provider's own lateness, not to a single global guess.
#:
#: * ``pendo``   600s. Pendo's aggregation API is documented at roughly five minutes of latency, so
#:   anything under that drops events that simply had not been aggregated when we asked. Ten minutes
#:   is that documented figure with the same doubling of slack the cadence comment applies.
#: * ``segment`` / ``hubspot``  120s. Both deliver into their event APIs promptly; two minutes
#:   covers clock skew between us and them plus ordinary delivery jitter.
CURSOR_OVERLAP_S: dict[str, float] = {
    "segment": 120.0,
    "hubspot": 120.0,
    "pendo": 600.0,
}
DEFAULT_CURSOR_OVERLAP_S = 120.0


class CursorStore(Protocol):
    """What :class:`~gateway.integration_workers.IngestWorker` needs from a cursor store.

    A Protocol so a worker can be driven in tests with an in-memory dict and no Postgres, the same
    way every other collaborator on that class already is.
    """

    def get(self, tenant_id: str, connector_id: str) -> datetime | None: ...

    def advance(self, tenant_id: str, connector_id: str, cursor_at: datetime) -> datetime: ...


class IngestCursorStore:
    """Postgres-backed watermark over ``tenant_ingest_cursors`` (migration 0028).

    Mirrors :class:`gateway.tenant_integrations.TenantIntegrationStore`: a connection per call and
    tenant-scoped SQL, so a buggy caller cannot cross tenants.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str, connector_id: str) -> datetime | None:
        """The stored watermark, or ``None`` for "this pair has never run".

        ``None`` is the honest first-run state and the caller floors it at
        :data:`INITIAL_LOOKBACK_S`; it must never be read as "fetch everything".
        """
        if connector_id not in ALLOWED_CURSOR_CONNECTORS:
            raise ValueError(f"unknown connector_id '{connector_id}'")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT cursor_at FROM tenant_ingest_cursors
                WHERE tenant_id = %s AND connector_id = %s
                """,
                (tenant_id, connector_id),
            )
            row = cur.fetchone()
        return None if row is None else _as_utc(row[0])

    def advance(self, tenant_id: str, connector_id: str, cursor_at: datetime) -> datetime:
        """Move the watermark forward. Returns what the row holds AFTER the write.

        MONOTONIC by construction: the upsert takes ``greatest(existing, proposed)``, so a cycle
        that computes an earlier watermark than one already stored (a clock skew between replicas,
        or a retry of an older window) cannot rewind the cursor and cause the next cycle to re-pull
        a window that was already handled. Rewinding would be safe for correctness, since
        re-insertion is idempotent, but it would silently undo the write amplification fix, which is
        the entire point of the table.
        """
        if connector_id not in ALLOWED_CURSOR_CONNECTORS:
            raise ValueError(f"unknown connector_id '{connector_id}'")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_ingest_cursors (tenant_id, connector_id, cursor_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE
                  SET cursor_at = greatest(tenant_ingest_cursors.cursor_at, EXCLUDED.cursor_at),
                      updated_at = now()
                RETURNING cursor_at
                """,
                (tenant_id, connector_id, _as_utc(cursor_at)),
            )
            row = cur.fetchone()
            conn.commit()
        assert row is not None
        return _as_utc(row[0])


class NullCursorStore:
    """The no-cursor fallback: always "never run", and every advance is dropped.

    Exists so an :class:`~gateway.integration_workers.IngestWorker` constructed without a store (a
    unit test of a mapper, say) still works. It is NOT the production path: with this store every
    cycle re-pulls the initial window, which is the pre-CTO-219 behaviour and is why
    :func:`gateway.worker_jobs.register_worker_jobs` builds a real
    :class:`IngestCursorStore` by default.
    """

    __slots__ = ()

    def get(self, tenant_id: str, connector_id: str) -> datetime | None:
        return None

    def advance(self, tenant_id: str, connector_id: str, cursor_at: datetime) -> datetime:
        return cursor_at


def overlap_for(connector_id: str) -> float:
    """Per-connector cursor overlap in seconds. See :data:`CURSOR_OVERLAP_S`."""
    return CURSOR_OVERLAP_S.get(connector_id, DEFAULT_CURSOR_OVERLAP_S)


def next_cursor(connector_id: str, window_end: datetime) -> datetime:
    """Where the cursor lands after a cycle whose window ended at ``window_end``.

    ``window_end`` minus this connector's overlap, so the next cycle re-asks for the tail the
    provider may not have aggregated yet. See the module docstring for why that is the safe
    direction to be wrong in.
    """
    return _as_utc(window_end) - timedelta(seconds=overlap_for(connector_id))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc
    )


__all__ = [
    "ALLOWED_CURSOR_CONNECTORS",
    "CURSOR_OVERLAP_S",
    "DEFAULT_CURSOR_OVERLAP_S",
    "INITIAL_LOOKBACK_S",
    "CursorStore",
    "IngestCursorStore",
    "NullCursorStore",
    "next_cursor",
    "overlap_for",
]
