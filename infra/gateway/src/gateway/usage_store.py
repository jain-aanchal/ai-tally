# SPDX-License-Identifier: Apache-2.0
"""Durable, shared billing usage behind ``GET /v1/usage`` (CTO-390).

WHY. Billing usage lived in one process's memory. The lifespan built a single ``UsageRollup()``,
``/v1/usage`` read that object directly, and :mod:`gateway.metering` is pure in-memory dicts with no
storage under them. Production runs more than one replica (``deploy/aws/ecs/gateway.service.json``
desiredCount 2, both Helm charts replicaCount 2, Cloud Run 1 to 10), so the number a tenant saw was a
fraction of their actual usage, the fraction depended on which replica answered, and a deploy reset
it to zero. Three different wrong answers to the question a bill is computed from.

THE SHAPE, and why it is two sources rather than one.

  * THE OPEN PERIOD is counted on read with ``uniqExact`` over ``otel_spans``
    (:meth:`gateway.store.ClickHouseStore.usage_counts`). ClickHouse already holds one row per span,
    is already shared by every replica, and already does exact distinct-counting. Nothing needs to be
    written on the ingest hot path for this, and every replica necessarily agrees because they are
    all reading the same table.
  * A CLOSED PERIOD is read from ``tenant_usage_periods`` (migration 0034). A committed figure must
    not move: ``otel_spans`` carries a 90-day TTL, so an old period counted live would drift toward
    zero, and a late backfill would otherwise change a month that has already been invoiced.

:class:`UsageRollup` keeps its job as the HEAD meter on the ingest path (CTO-84) and as the owner of
plan limits, which are configuration rather than telemetry. It is no longer what ``/v1/usage`` reads.

WHAT HAPPENS WHEN A SOURCE IS UNAVAILABLE. :class:`UsageUnavailable` propagates and the endpoint
answers 503 with explicit nulls. It never falls back to the in-process meter and never returns 0.
This is the whole point of the ticket: a small wrong number is indistinguishable from a real small
number, so a tenant reading "you have used 12 traces" during a Postgres blip has no way to know they
are looking at a lie. An error says so. Note that Postgres being unreachable fails the read even
though ClickHouse could answer: without the committed table we cannot tell whether this period is
frozen, so a live count might silently contradict an already-issued invoice.

THE CACHE. :class:`DurableUsageRollup` holds a short in-process TTL cache in FRONT of the durable
sources, so a polling dashboard does not run a ``uniqExact`` over a tenant-month every few seconds.
It is a cache and behaves like one: a miss goes to the durable source, and a durable source that
cannot answer produces an error rather than a stale or empty entry. A committed period is cached
without expiry, because immutable is exactly what it is.

FOLLOW-UP, stated plainly rather than half-wired: nothing calls :meth:`CommittedUsageStore.commit`
on a schedule yet. Closing a period is a billing-cycle job (a scheduler job in the CTO-213 registry),
and it is deliberately not in this change. Until it lands, every period reads live from ClickHouse,
which is correct for the current month and correct for any month inside the 90-day TTL, and the read
path already prefers a committed row the moment one exists.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

import psycopg

from gateway.config import Settings
from gateway.metering import DEFAULT_PLAN_LIMIT, PlanLimit, UsageRecord, billing_period

logger = logging.getLogger("tally.gateway.usage_store")

#: Error code returned by ``/v1/usage`` when no durable source could answer. Deliberately NOT added
#: to :class:`gateway.errors.ErrorCode`: that enum is the ingest rejection contract that SDK clients
#: branch on, and this is a read endpoint for the dashboard and billing.
USAGE_UNAVAILABLE_CODE = "USAGE_UNAVAILABLE"


class UsageUnavailable(RuntimeError):
    """No durable source could answer, so there is no honest number to return.

    The caller MUST surface this as an error or an explicit unknown. Falling back to the in-process
    meter, or to zero, reintroduces exactly the bug this module exists to fix.
    """


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` for a ``YYYY-MM`` billing period.

    Half-open on purpose: a span at midnight on the first of a month belongs to exactly one period,
    so no span is billed twice and none falls between two periods.

    Raises :class:`ValueError` on anything that is not a real month, which the endpoint turns into a
    422. Parsing this loosely would silently bill a caller for a window nobody asked about.
    """
    parts = period.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"period must be YYYY-MM with a real month, got {period!r}")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return start, end


class UsageCountSource(Protocol):
    """What the open period needs: exact distinct counts over a tenant's spans in a window."""

    def usage_counts(
        self, tenant_id: str, *, period_start: datetime, period_end: datetime
    ) -> tuple[int, int]: ...


class CommittedUsageStore:
    """Postgres-backed committed usage over ``tenant_usage_periods`` (migration 0034).

    Connection per call and tenant-scoped SQL, matching
    :class:`gateway.batch_idempotency.PostgresIdempotencyStore` and every other control-plane store.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str, period: str) -> UsageRecord | None:
        """The committed record for a period, or ``None`` if the period was never committed.

        ``None`` means "not frozen yet", which is a normal state for the current month and is what
        sends the caller to the live count. It never means zero, and the two must not be conflated:
        a committed row of 0 is a claim that the period really had no usage.

        Raises :class:`UsageUnavailable` if Postgres cannot answer, because "I could not check
        whether this period is frozen" is not the same as "it is not frozen".
        """
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trace_count, feature_count, trace_commitment, feature_commitment,
                           plan, trace_limit, feature_limit
                      FROM tenant_usage_periods
                     WHERE tenant_id = %s AND period = %s
                    """,
                    (tenant_id, period),
                )
                row = cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise UsageUnavailable(f"committed usage store unavailable: {exc}") from exc

        if row is None:
            return None
        return UsageRecord(
            tenant_id=tenant_id,
            period=period,
            trace_count=int(row[0]),
            feature_count=int(row[1]),
            trace_commitment=row[2],
            feature_commitment=row[3],
            plan=str(row[4]),
            trace_limit=None if row[5] is None else int(row[5]),
            feature_limit=None if row[6] is None else int(row[6]),
            closed=True,
        )

    def commit(self, record: UsageRecord) -> None:
        """Freeze a period's figure. Idempotent, and the FIRST commit wins.

        ``DO NOTHING`` rather than an upsert, and that is the whole contract: a committed period is
        immutable (CTO-86). Overwriting one would let a late backfill, or a re-run of the closing
        job, silently change a month that has already been billed. A caller that believes the stored
        figure is wrong has to delete it deliberately, which is a decision a person makes.

        Raises :class:`UsageUnavailable` if Postgres cannot answer, so a closing job records a
        failure and emits nothing rather than reporting a period closed that is not.
        """
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tenant_usage_periods
                        (tenant_id, period, trace_count, feature_count, trace_commitment,
                         feature_commitment, plan, trace_limit, feature_limit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, period) DO NOTHING
                    """,
                    (
                        record.tenant_id,
                        record.period,
                        record.trace_count,
                        record.feature_count,
                        record.trace_commitment,
                        record.feature_commitment,
                        record.plan,
                        record.trace_limit,
                        record.feature_limit,
                    ),
                )
                conn.commit()
        except psycopg.Error as exc:
            raise UsageUnavailable(f"committed usage store unavailable: {exc}") from exc


class DurableUsageRollup:
    """What ``/v1/usage`` reads: committed figure if frozen, else an exact live count.

    Drop-in for the read half of :class:`gateway.metering.UsageRollup` (same ``usage`` signature), so
    the endpoint needed no new shape and a caller cannot accidentally keep reading the old one.
    """

    def __init__(
        self,
        *,
        counts_source: Callable[[], UsageCountSource],
        committed: CommittedUsageStore | None,
        plan_limits: Callable[[str], PlanLimit] | None = None,
        cache_ttl_s: float = 15.0,
        now_s: Callable[[], float] | None = None,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        # A callable, not the store itself: app.state.store is swapped by the test suite and could be
        # replaced on a reconnect, and a captured reference would quietly go on reading a dead client.
        self._counts_source = counts_source
        self._committed = committed
        self._plan_limits = plan_limits or (lambda _tenant_id: DEFAULT_PLAN_LIMIT)
        self._cache_ttl_s = cache_ttl_s
        self._now_s = now_s or time.monotonic
        self._now_ns = now_ns or time.time_ns
        self._cache: dict[tuple[str, str], tuple[float, UsageRecord]] = {}

    def usage(self, tenant_id: str, period: str | None = None) -> UsageRecord:
        """Usage for ``period`` (defaults to the tenant's current period).

        Raises :class:`ValueError` for a malformed period and :class:`UsageUnavailable` when no
        durable source can answer. It never returns a partial or in-process number.
        """
        if period is None:
            period = billing_period(self._now_ns())
        period_start, period_end = period_bounds(period)

        key = (tenant_id, period)
        cached = self._cache.get(key)
        if cached is not None and self._now_s() < cached[0]:
            return cached[1]

        if self._committed is not None:
            frozen = self._committed.get(tenant_id, period)
            if frozen is not None:
                # Immutable by definition, so it never expires from the cache.
                self._cache[key] = (math.inf, frozen)
                return frozen

        source = self._counts_source()
        try:
            trace_count, feature_count = source.usage_counts(
                tenant_id, period_start=period_start, period_end=period_end
            )
        except UsageUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any read failure is an unanswerable read
            raise UsageUnavailable(f"usage count source unavailable: {exc}") from exc

        limit = self._plan_limits(tenant_id)
        record = UsageRecord(
            tenant_id=tenant_id,
            period=period,
            trace_count=trace_count,
            feature_count=feature_count,
            # Not computed for a live count, and said so rather than faked. See migration 0034.
            trace_commitment=None,
            feature_commitment=None,
            plan=limit.plan,
            trace_limit=limit.trace_limit,
            feature_limit=limit.feature_limit,
            closed=False,
        )
        self._cache[key] = (self._now_s() + self._cache_ttl_s, record)
        return record


__all__ = [
    "USAGE_UNAVAILABLE_CODE",
    "CommittedUsageStore",
    "DurableUsageRollup",
    "UsageCountSource",
    "UsageUnavailable",
    "period_bounds",
]
