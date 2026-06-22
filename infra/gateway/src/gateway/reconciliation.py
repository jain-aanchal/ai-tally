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
  matched span timestamps, calls the pure compute, and records the run. Per-tenant scheduling / a
  running daemon is out of scope (CTO-139) — a callable orchestrator is enough.

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


class _ClickHouseEventSource(Protocol):
    """Minimal surface the orchestrator needs from a ClickHouse client.

    Kept as a Protocol so :func:`run_reconciliation` is trivially fakeable in tests without standing
    up ClickHouse — the unit-testable core is :func:`compute_late_arrivals`; this orchestrator just
    glues a CH scan to the store.
    """

    def fetch_event_span_pairs(
        self, tenant_id: str
    ) -> Sequence[tuple[datetime, datetime]]: ...


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

    The CH query itself is intentionally simple — per-tenant scheduling and a smarter matched-span
    join are out of scope (CTO-139); the value here is the pure compute + the run log.
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
