"""Reconciler pipeline + late-arrival tracking (CTO-139).

The /features "Attribution diagnostics" card surfaces three tenant-wide signals — how many value
events arrived "late", the median lateness, and how long ago the reconciler last ran. Before this
ticket those were honestly hardcoded zero because no reconciler existed. This module is the real
pipeline:

* :func:`compute_late_arrivals` is the pure, unit-testable core. Given a set of business events and
  the timestamp of each event's matched span, it counts the "late" events (event ``OccurredAt`` more
  than :data:`LATE_THRESHOLD_SECONDS` after the matched span ``Timestamp``) and returns the lag
  distribution (median + p95) over those late events.
* :class:`ReconciliationStore` is a tiny Postgres-backed CRUD over ``reconciliation_runs`` mirroring
  :mod:`gateway.tenant_integrations` — ``record_run`` stamps the outcome of one pass, ``get_latest``
  reads the most recent for the dashboard.
* :func:`run_reconciliation` is a thin orchestrator: it queries ClickHouse for recent events + their
  matched span timestamps, calls the pure compute, and records the run.
* :class:`ClickHouseLateArrivalSource` is that CH scan (CTO-216). CTO-139 shipped the orchestrator
  with no real source because nothing ran the reconciler; the scheduled job needs one to record
  anything other than zeros. It is last-touch pairing at read time, NOT attribution: it writes
  nothing to ``attribution_records`` and credits no feature with any value.

Per-tenant scheduling is CTO-216: the scheduler registers ``reconciliation`` as a job and calls
:func:`run_reconciliation` per tenant on a cadence.

Why a separate table from ``tenant_integration_runs`` — that's a *third-party integration* run log
("Stripe last fired 12s ago"). This is the *reconciler* run log ("we re-checked attribution and 180
events arrived late"). Different questions, different tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, Sequence

import psycopg

from gateway.config import Settings

logger = logging.getLogger("tally.gateway.reconciliation")

# An event is "late" when it arrived (OccurredAt) more than this many seconds after the span it
# attributes to. One hour is the threshold the /features card is documented against.
LATE_THRESHOLD_SECONDS = 3600

RunStatus = Literal["ok", "partial", "failed"]
_ALLOWED_STATUSES: frozenset[str] = frozenset({"ok", "partial", "failed"})


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over a pre-sorted, non-empty sequence (q in [0, 1])."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return float(sorted_vals[idx])


def compute_late_arrivals(
    events: Sequence[tuple[datetime, datetime]],
) -> tuple[int, int, int]:
    """Pure late-arrival stats over (event_occurred_at, matched_span_ts) pairs.

    ``events`` is a sequence of ``(occurred_at, span_ts)`` — a business event's ``OccurredAt`` and
    the ``Timestamp`` of the span it matched. An event is *late* when ``occurred_at`` is more than
    :data:`LATE_THRESHOLD_SECONDS` after ``span_ts`` (i.e. the value event landed well after the
    work that produced it).

    Returns ``(events_late, median_lag_s, p95_lag_s)`` where the lag stats are computed over the
    *late* events only (the ones that breached the threshold), as whole seconds. With no late
    events, returns ``(0, 0, 0)``.

    Pure and side-effect-free — this is the unit-testable heart of the pipeline.
    """
    lags: list[float] = []
    for occurred_at, span_ts in events:
        lag = (occurred_at - span_ts).total_seconds()
        if lag > LATE_THRESHOLD_SECONDS:
            lags.append(lag)
    if not lags:
        return (0, 0, 0)
    lags.sort()
    median = _percentile(lags, 0.5)
    p95 = _percentile(lags, 0.95)
    return (len(lags), int(round(median)), int(round(p95)))


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    """The dashboard's view of one reconciliation pass. ``None`` when a tenant has never run one."""

    started_at: str
    finished_at: str
    events_late: int
    lag_seconds_median: int
    lag_seconds_p95: int
    status: RunStatus

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "events_late": self.events_late,
            "lag_seconds_median": self.lag_seconds_median,
            "lag_seconds_p95": self.lag_seconds_p95,
            "status": self.status,
        }


class ReconciliationStore:
    """Tiny Postgres-backed CRUD over ``reconciliation_runs``.

    Tenant-scoped — every query takes ``tenant_id`` from upstream auth so a buggy caller can't cross
    tenants. Mirrors :class:`gateway.tenant_integrations.TenantIntegrationStore`.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get_latest(self, tenant_id: str) -> ReconciliationRun | None:
        """Return the most recent reconciliation run for the tenant, or ``None`` if none exist.

        ``None`` is the honest "no reconciler run yet" state — the dashboard surfaces it as a
        stale / em-dash card and the web fn falls back to its mock.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT started_at, finished_at, events_late,
                       lag_seconds_median, lag_seconds_p95, status
                FROM reconciliation_runs
                WHERE tenant_id = %s
                ORDER BY finished_at DESC
                LIMIT 1
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ReconciliationRun(
                started_at=row[0].isoformat(),
                finished_at=row[1].isoformat(),
                events_late=int(row[2]),
                lag_seconds_median=int(row[3]),
                lag_seconds_p95=int(row[4]),
                status=row[5],  # CHECK constraint guarantees the literal
            )

    def record_run(
        self,
        tenant_id: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        events_late: int,
        lag_seconds_median: int,
        lag_seconds_p95: int,
        status: RunStatus,
    ) -> ReconciliationRun:
        """Append the outcome of one reconciliation pass and return it.

        Append-only (one row per pass) so the run history is retained; ``get_latest`` reads the
        newest. Validates ``status`` and non-negative metrics before binding the SQL params.
        """
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"unknown status '{status}'")
        if events_late < 0 or lag_seconds_median < 0 or lag_seconds_p95 < 0:
            raise ValueError("late-arrival metrics must be non-negative")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reconciliation_runs
                    (tenant_id, started_at, finished_at, events_late,
                     lag_seconds_median, lag_seconds_p95, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING started_at, finished_at, events_late,
                          lag_seconds_median, lag_seconds_p95, status
                """,
                (
                    tenant_id,
                    started_at,
                    finished_at,
                    events_late,
                    lag_seconds_median,
                    lag_seconds_p95,
                    status,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            assert row is not None
            return ReconciliationRun(
                started_at=row[0].isoformat(),
                finished_at=row[1].isoformat(),
                events_late=int(row[2]),
                lag_seconds_median=int(row[3]),
                lag_seconds_p95=int(row[4]),
                status=row[5],
            )


# How far back one reconciliation pass looks, and the ceiling on how many events it examines.
# Both exist to bound the scan: this runs on a cadence now (CTO-216) rather than by hand, so an
# unbounded "every event this tenant ever sent" query would get slower every day until it was the
# most expensive thing the gateway does. Seven days is wider than the one-hour lateness threshold by
# a large margin, so nothing a pass could sensibly call "late" falls outside it.
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_EVENT_CAP = 50_000

# Slack on the span side of the join, in days (CTO-219). An event at the very start of the event
# window still has to be able to find the span that preceded it, so the span window opens EARLIER
# than the oldest event in the pass. One day is the smallest slack that is still far wider than
# LATE_THRESHOLD_SECONDS, and otel_spans is PARTITION BY toDate(Timestamp), so a whole number of
# days is also exactly the granularity at which part pruning happens.
DEFAULT_SPAN_SLACK_DAYS = 1

# Hard server-side ceilings on one pass (CTO-219). These are ClickHouse query settings, not Python
# ones: they are what makes a runaway pass FAIL rather than consume the ClickHouse server, and a
# failure here is recorded as a `failed` run with zeroed metrics, so the /features card reads
# "ran, but errored" and never a wrong lateness figure.
#
# max_rows_to_read with read_overflow_mode='throw' is the load-bearing one. It bounds rows SCANNED,
# which is the thing the old `LIMIT 50000` did NOT do: a LIMIT caps rows RETURNED, and the ASOF join
# materialises its right-hand side in full before any limit applies. 50 million rows is roughly two
# orders of magnitude more than a correctly-pruned pass should touch, so tripping it means the
# pruning is broken rather than that the tenant is merely large.
DEFAULT_MAX_ROWS_TO_READ = 50_000_000
DEFAULT_MAX_MEMORY_BYTES = 2 * 1024**3  # 2 GiB
DEFAULT_MAX_EXECUTION_S = 120


class _ClickHouseEventSource(Protocol):
    """Minimal surface the orchestrator needs from a ClickHouse client.

    Kept as a Protocol so :func:`run_reconciliation` is trivially fakeable in tests without standing
    up ClickHouse — the unit-testable core is :func:`compute_late_arrivals`; this orchestrator just
    glues a CH scan to the store.
    """

    def fetch_event_span_pairs(
        self, tenant_id: str
    ) -> Sequence[tuple[datetime, datetime]]: ...


class ClickHouseLateArrivalSource:
    """The real :class:`_ClickHouseEventSource`: pairs each value event with the span it followed.

    WHY this exists (CTO-216). CTO-139 shipped the pure compute, the run log and the orchestrator,
    and left the CH scan to whoever first ran the reconciler on a schedule. That is this ticket, so
    the scan has to exist for the job to record anything but zeros.

    WHAT "the span it matched" means here, precisely, because the honest answer is narrow: the most
    recent span for the SAME hashed user at or before the event. That is last-touch, evaluated at
    read time, and it is deliberately NOT attribution. Nothing is written to ``attribution_records``
    and no feature is credited with any value. Real attribution needs the stitcher runner (CTO-200),
    which does not exist; this query answers only the much smaller question the /features
    diagnostics card actually asks, which is "how long after the work did the value show up".

    The ``ASOF INNER JOIN`` is what expresses "at or before": ClickHouse resolves it to the nearest
    preceding row per key. INNER, so an event with no preceding span for that user contributes
    nothing rather than a fabricated lag of zero — an event we cannot pair is not an event that
    arrived on time.

    BOUNDING THE SCAN (CTO-219). The first cut of this query capped the EVENT side with
    ``LIMIT 50000`` and left the span side as a flat "the last 14 days of otel_spans for this
    tenant". That is not a bound. A ``LIMIT`` caps rows RETURNED; an ASOF join materialises its
    right-hand side before any limit on the result can apply, so the span side was read in full
    every hour. On a high-volume tenant that is ``MEMORY_LIMIT_EXCEEDED`` every single hour, which
    means the diagnostics card never updates for exactly the tenants it matters most for. Three
    things fix it, in order of how much they buy:

    1. **The span window is derived from the events, not from the clock.** The span side is
       restricted to ``[min(event OccurredAt) - slack, max(event OccurredAt)]``, read from the
       already-capped event set via scalar subqueries. ClickHouse evaluates a scalar subquery first
       and substitutes the constant, so these become literal bounds on ``Timestamp`` and prune parts
       (``otel_spans`` is ``PARTITION BY toDate(Timestamp)``). This is what actually collapses the
       scan: a busy tenant hits the 50k event cap inside a few minutes of wall clock, so the span
       side goes from fourteen days of parts to one or two. A quiet tenant's events genuinely span
       the whole window and it reads the whole window, which is correct and is also cheap.
    2. **The fixed lookback is kept as a floor, not as the bound.** ``Timestamp >= subtractDays(...)``
       stays, so if the derived bound is ever wrong the query is still no worse than it was.
    3. **Server-side ceilings** (see :data:`DEFAULT_MAX_ROWS_TO_READ` and friends) so a pass that
       escapes both bounds fails fast and honestly instead of taking ClickHouse, and with it the
       gateway, down. ``run_reconciliation`` turns that raise into a ``failed`` run with zeroed
       metrics: the card goes stale-but-labelled, and never shows a wrong lateness figure.

    NOT SAMPLED, deliberately. ``SAMPLE`` on the span side would be the cheapest bound of all and it
    would silently corrupt the answer: dropping spans makes the ASOF join match an EARLIER span than
    the true nearest one, so every sampled pass would over-report lateness. A bound that changes the
    number is not a bound on this query, it is a different query.
    """

    _SQL = """
        WITH capped_events AS (
            SELECT UserIdHash, OccurredAt
            FROM business_events
            WHERE TenantId = {tenant:String}
              AND UserIdHash != ''
              AND OccurredAt >= subtractDays(now64(9), {days:UInt32})
            ORDER BY OccurredAt DESC
            LIMIT {cap:UInt32}
        )
        SELECT e.OccurredAt, s.Timestamp
        FROM capped_events AS e
        ASOF INNER JOIN (
            SELECT UserIdHash, Timestamp
            FROM otel_spans
            WHERE TenantId = {tenant:String}
              AND UserIdHash != ''
              AND Timestamp >= subtractDays(now64(9), {span_days:UInt32})
              AND Timestamp >= subtractDays(
                      (SELECT min(OccurredAt) FROM capped_events), {span_slack_days:UInt32})
              AND Timestamp <= (SELECT max(OccurredAt) FROM capped_events)
        ) AS s
        ON e.UserIdHash = s.UserIdHash AND e.OccurredAt >= s.Timestamp
    """

    def __init__(
        self,
        store: object,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        event_cap: int = DEFAULT_EVENT_CAP,
        span_slack_days: int = DEFAULT_SPAN_SLACK_DAYS,
        max_rows_to_read: int = DEFAULT_MAX_ROWS_TO_READ,
        max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
        max_execution_s: int = DEFAULT_MAX_EXECUTION_S,
    ) -> None:
        if lookback_days <= 0 or event_cap <= 0:
            raise ValueError("lookback_days and event_cap must be positive")
        if span_slack_days <= 0:
            raise ValueError("span_slack_days must be positive")
        if max_rows_to_read <= 0 or max_memory_bytes <= 0 or max_execution_s <= 0:
            raise ValueError("query ceilings must be positive")
        self._store = store
        self._lookback_days = int(lookback_days)
        self._event_cap = int(event_cap)
        self._span_slack_days = int(span_slack_days)
        self._settings: dict[str, object] = {
            # Rows SCANNED, not rows returned. See DEFAULT_MAX_ROWS_TO_READ.
            "max_rows_to_read": int(max_rows_to_read),
            "read_overflow_mode": "throw",
            "max_memory_usage": int(max_memory_bytes),
            # A pass that is still running after this is not going to produce a number anyone
            # wants; the next cadence window is an hour away and will try again.
            "max_execution_time": int(max_execution_s),
            "timeout_overflow_mode": "throw",
        }

    @property
    def query_settings(self) -> dict[str, object]:
        """The ClickHouse-side ceilings this source enforces. Exposed so tests can assert on them."""
        return dict(self._settings)

    def fetch_event_span_pairs(self, tenant_id: str) -> Sequence[tuple[datetime, datetime]]:
        """One tenant-scoped round trip. Raises on a CH failure, which records a ``failed`` run.

        ``throw`` on both overflow modes is the point: an exception here becomes a ``failed`` run,
        which is the honest outcome. The alternative modes (``break``) would return a partial result
        that looks exactly like a complete one, and the card would show a confidently wrong number.
        """
        result = self._store.client.query(  # type: ignore[attr-defined]
            self._SQL,
            parameters={
                "tenant": tenant_id,
                "days": self._lookback_days,
                # The floor under the derived span bound, not the bound itself. See the class
                # docstring: without a derived bound this alone read the whole window every hour.
                "span_days": self._lookback_days + self._span_slack_days,
                "span_slack_days": self._span_slack_days,
                "cap": self._event_cap,
            },
            settings=self._settings,
        )
        return [(_as_utc(row[0]), _as_utc(row[1])) for row in result.result_rows]


def _as_utc(value: datetime) -> datetime:
    """ClickHouse hands back naive UTC for a ``DateTime64`` with no timezone. Make that explicit.

    Both halves of a pair come from the same query and are therefore always both naive or both
    aware, so the subtraction in :func:`compute_late_arrivals` would work either way. Stamping UTC
    keeps a caller that mixes these with a Python-side timestamp from getting a TypeError.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def run_reconciliation(
    ch_source: _ClickHouseEventSource,
    pg_store: ReconciliationStore,
    tenant_id: str,
) -> ReconciliationRun:
    """Run one reconciliation pass for a tenant and record it.

    Thin orchestrator: pull ``(event_occurred_at, matched_span_ts)`` pairs from ClickHouse, run the
    pure :func:`compute_late_arrivals`, and persist the run. ``status`` is ``"failed"`` if the CH
    scan raises (recorded with zeroed metrics so the dashboard shows "ran, but errored" rather than
    silently going stale), else ``"ok"``.

    A smarter matched-span join (real attribution) is out of scope and belongs to the stitcher
    runner, CTO-200. What IS in scope, since CTO-219, is that the scan the source performs is
    bounded: a pass that cannot compute a number records ``failed`` and writes zeroed metrics, so
    the /features card goes stale-but-labelled rather than showing a lateness figure nobody
    measured.
    """
    started_at = datetime.now(tz=timezone.utc)
    status: RunStatus = "ok"
    events_late = median_lag = p95_lag = 0
    try:
        pairs = ch_source.fetch_event_span_pairs(tenant_id)
        events_late, median_lag, p95_lag = compute_late_arrivals(pairs)
    except Exception as exc:  # noqa: BLE001 — a failed scan must still record a (failed) run
        logger.warning("reconciliation scan failed for tenant %s: %s", tenant_id, exc)
        status = "failed"
    finished_at = datetime.now(tz=timezone.utc)
    return pg_store.record_run(
        tenant_id,
        started_at=started_at,
        finished_at=finished_at,
        events_late=events_late,
        lag_seconds_median=median_lag,
        lag_seconds_p95=p95_lag,
        status=status,
    )
