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

THE CTO-84 HEAD-COUNT PROPERTY, per ingest mode, because it is NOT preserved in both and an earlier
draft of this module claimed it was. CTO-84 requires that sampling analytics down must never reduce
the billed count.

  * SYNCHRONOUS ingest (the default): preserved. ``head_sample_rate`` is only stamped onto the
    written row and never drops a span at the gateway, backpressure shedding and per-item validation
    both run BEFORE the head measurement, and spans dropped by client-side sampling never reach the
    gateway at all. Nothing removes a span between the head measurement and the ClickHouse write, so
    a ``uniqExact`` over what was written equals what the head meter counted.
  * BUFFERED ingest (``TALLY_INGEST_BUFFERED=true``): NOT preserved, in both directions. Rows past
    the buffer's high-water mark are shed after the head meter has seen them, and the in-memory
    buffer is lost outright on a restart, so spans the head meter counted may never reach
    ``otel_spans`` and ``uniqExact`` UNDER-reports against CTO-84. What the number means in this
    mode is "distinct traces that reached storage", which is the honest thing to bill for but is not
    the same quantity the head meter reports. The two legitimately disagree here and reconciling
    them is its own piece of work, not this module's.

The general caveat behind both: this equivalence rests on nothing dropping a span between the head
meter and the durable write. If tail sampling or drop-on-write is introduced, the ClickHouse count
starts undercounting the bill on every path and the head meter would have to become the persisted
source instead.

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

WHAT THIS TABLE DOES NOT STORE, AND WHY THAT IS A REFUSAL RATHER THAN A COLUMN (CTO-401 review).
``tenant_usage_periods`` has no exactness column, so a :class:`~gateway.metering.UsageRecord` whose
count is a FLOOR (the head meter's id set saturated its cap, ``trace_count_exact=False``) would be
written as a plain number and read back by :meth:`CommittedUsageStore.get` as exact, which
reconstructs the record with the ``True`` default. The floor would become a permanent, confident
figure in a table that is immutable by design, and immutable is exactly what makes that
unrecoverable: the row cannot be corrected later.

The two ways out were a new nullable column or a refusal to commit at all. This module takes the
REFUSAL, because the column would be dead weight on a path that should never produce such a record
in the first place: the durable count is a ClickHouse ``uniqExact``, which is exact by construction,
so the only producer of an inexact record is the in-process head meter, and that meter is not what
``/v1/usage`` or a closing job reads. Storing an unknown-quality figure permanently, to support a
caller that should not exist, is the worse of the two. :meth:`CommittedUsageStore.commit` therefore
raises on an inexact record and the closing job records a failure and emits nothing, which is the
same shape every other job here takes under uncertainty. The fix for a tenant hitting it is to raise
``TALLY_METERING_MAX_IDS_PER_PERIOD``, which :func:`gateway.metering.validate_max_ids_per_period`
already bounds from below at boot.

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
from datetime import datetime, timedelta, timezone
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
        # CTO-390 review: every connect here is bounded. This store opens a connection per call from
        # a SYNC endpoint handler running on the threadpool, so an unbounded connect or an unbounded
        # query against a black-holed Postgres would pin a worker and the promised explicit-unknown
        # 503 would never arrive. connect_timeout bounds the handshake and statement_timeout bounds a
        # query that connected and then stalled; one without the other still leaves a way to hang.
        self._connect_kwargs: dict[str, object] = {
            "connect_timeout": settings.postgres_connect_timeout_s,
            "options": f"-c statement_timeout={int(settings.postgres_statement_timeout_ms)}",
        }

    def probe(self) -> str | None:
        """``None`` if ``tenant_usage_periods`` is readable, else a one-line reason why not.

        CTO-390 review. Without this the first symptom of a missing migration 0034 is an
        undifferentiated 503 from ``/v1/usage`` with the cause only in a log line nobody is reading.
        ``docker-entrypoint-initdb.d`` fires only on a first boot against an empty volume, so EVERY
        already-running stack lacks this table until 0034 is applied by hand, which makes "the table
        is simply not there" the common case rather than an exotic one. Mirrors
        :func:`gateway.batch_idempotency.build_batch_idempotency`, which probes at boot and states
        its mode.
        """
        try:
            with psycopg.connect(self._dsn, **self._connect_kwargs) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM tenant_usage_periods LIMIT 1")
                cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return None

    def get(self, tenant_id: str, period: str) -> UsageRecord | None:
        """The committed record for a period, or ``None`` if the period was never committed.

        ``None`` means "not frozen yet", which is a normal state for the current month and is what
        sends the caller to the live count. It never means zero, and the two must not be conflated:
        a committed row of 0 is a claim that the period really had no usage.

        Raises :class:`UsageUnavailable` if Postgres cannot answer, because "I could not check
        whether this period is frozen" is not the same as "it is not frozen".
        """
        try:
            with psycopg.connect(self._dsn, **self._connect_kwargs) as conn, conn.cursor() as cur:
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

        Raises :class:`ValueError` on a record whose counts are a FLOOR rather than the figure
        (CTO-401 review). ``tenant_usage_periods`` has no exactness column, so such a row would be
        stored as a plain number and read back as exact by :meth:`get`, freezing an
        unknown-quality count into a table that cannot be corrected. Refusing is the honest move and
        costs nothing real: the durable count is a ClickHouse ``uniqExact`` and is exact by
        construction, so only the in-process head meter can produce an inexact record. See this
        module's docstring for why this is a refusal and not a new column.
        """
        if not (record.trace_count_exact and record.feature_count_exact):
            raise ValueError(
                f"refusing to commit period {record.period} for {record.tenant_id}: its counts are "
                "a floor, not the figure (the head meter's id set saturated its cap), and "
                "tenant_usage_periods cannot record that qualification. Raise "
                "TALLY_METERING_MAX_IDS_PER_PERIOD and recount rather than freezing a number "
                "nobody can later tell apart from an exact one"
            )
        try:
            with psycopg.connect(self._dsn, **self._connect_kwargs) as conn, conn.cursor() as cur:
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
        live_count_retention_days: int = 90,
        boot_warning: str | None = None,
        now_s: Callable[[], float] | None = None,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        # A callable, not the store itself: app.state.store is swapped by the test suite and could be
        # replaced on a reconnect, and a captured reference would quietly go on reading a dead client.
        self._counts_source = counts_source
        self._committed = committed
        self._plan_limits = plan_limits or (lambda _tenant_id: DEFAULT_PLAN_LIMIT)
        self._cache_ttl_s = cache_ttl_s
        self._retention_days = live_count_retention_days
        #: Set by :func:`build_usage_rollup` when the boot probe found the committed table missing,
        #: so a 503 can name the cause instead of being undifferentiated (CTO-390 review).
        self.boot_warning = boot_warning
        self._now_s = now_s or time.monotonic
        self._now_ns = now_ns or time.time_ns
        self._cache: dict[tuple[str, str], tuple[float, UsageRecord]] = {}

    def current_period(self) -> str:
        """The period a bare ``/v1/usage`` call means, so a failed read can still name which one."""
        return billing_period(self._now_ns())

    def _guard_retention(self, period: str, period_start: datetime) -> None:
        """Refuse to count a period whose raw spans have started aging out (CTO-390 review).

        ``otel_spans`` DELETEs raw rows at 90 days and deliberately does NOT aggregate on expire
        (``db/clickhouse/otel_spans.sql`` says so: the TTL GROUP BY keys are not a primary-key
        prefix). Nothing calls :meth:`CommittedUsageStore.commit` on a schedule yet, so an old period
        has no committed row to fall back to, and a live ``uniqExact`` over it returns a count of
        whatever rows survive: a number that shrinks a little every day and is served as ordinary
        open data with ``closed: false``. That is the exact failure this codebase refuses, a real
        figure decaying silently toward zero, so it is an explicit unknown instead.

        Only the LIVE count is guarded. A committed period is checked first and is answered from the
        frozen row however old it is, which is the whole reason that row exists.
        """
        horizon = datetime.fromtimestamp(self._now_ns() / 1e9, tz=timezone.utc) - timedelta(
            days=self._retention_days
        )
        if period_start < horizon:
            raise UsageUnavailable(
                f"period {period} is older than the {self._retention_days}-day raw span retention "
                "and was never committed, so no exact count exists for it"
            )

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

        self._guard_retention(period, period_start)
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


def build_usage_rollup(
    settings: Settings,
    *,
    counts_source: Callable[[], UsageCountSource],
    plan_limits: Callable[[str], PlanLimit] | None = None,
) -> DurableUsageRollup:
    """Wire the usage read path at boot, probing whether the committed table is actually there.

    CTO-390 review. ``CommittedUsageStore`` used to be constructed unconditionally, so a deployment
    missing migration 0034 discovered it as a 503 from ``/v1/usage`` with the reason buried in a log.
    That is a bad way to find out, and it is the COMMON case rather than an exotic one:
    ``docker-entrypoint-initdb.d`` fires only on a first boot against an empty volume, so every
    already-running stack lacks ``tenant_usage_periods`` until 0034 is applied by hand.

    So this states the mode at boot the way
    :func:`gateway.batch_idempotency.build_batch_idempotency` does, and carries the reason onto the
    rollup so the 503 body can name it. ``usage_durable_required`` turns the warning into a startup
    failure, which is what a deployment that serves ``/v1/usage`` should set.

    The store is still attached when the probe fails. Dropping it would leave the read path unable to
    tell a frozen period from an open one, and it would answer live counts for already-invoiced
    months, which is a worse failure than an error.
    """
    committed = CommittedUsageStore(settings)
    reason = committed.probe()
    if reason is not None:
        if settings.usage_durable_required:
            raise RuntimeError(
                "durable usage is required but tenant_usage_periods is unreachable "
                f"({reason}); apply db/postgres/0034_tenant_usage_periods.sql"
            )
        logger.warning(
            "committed usage table UNAVAILABLE (%s); GET /v1/usage will answer 503 with explicit "
            "nulls rather than a number, because without it a live count cannot be told apart from "
            "an already-invoiced period. Apply db/postgres/0034_tenant_usage_periods.sql (initdb "
            "only fires on a first boot against an empty volume), and set "
            "TALLY_USAGE_DURABLE_REQUIRED=true to make this a startup failure instead",
            reason,
        )
    else:
        logger.info("durable usage enabled (tenant_usage_periods)")
    return DurableUsageRollup(
        counts_source=counts_source,
        committed=committed,
        plan_limits=plan_limits,
        cache_ttl_s=settings.usage_cache_ttl_s,
        live_count_retention_days=settings.usage_live_count_retention_days,
        boot_warning=reason,
    )


__all__ = [
    "USAGE_UNAVAILABLE_CODE",
    "CommittedUsageStore",
    "DurableUsageRollup",
    "UsageCountSource",
    "UsageUnavailable",
    "build_usage_rollup",
    "period_bounds",
]
