"""Reconciler pipeline + late-arrival tracking (CTO-139).

These tests pin: the pure ``compute_late_arrivals`` core (the unit-testable heart), the
GET /v1/tenant/reconciliation/status endpoint (fresh tenant -> null run, recorded run reflected,
cross-tenant isolation, tenant required when auth is off), and the ``run_reconciliation``
orchestrator (happy path + a CH scan that raises records a failed run).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.reconciliation import (
    LATE_THRESHOLD_SECONDS,
    ReconciliationRun,
    compute_late_arrivals,
    run_reconciliation,
)

T = "t-acme"


def _pair(occurred_minutes_after_span: float) -> tuple[datetime, datetime]:
    span_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    occurred = span_ts + timedelta(minutes=occurred_minutes_after_span)
    return (occurred, span_ts)


# --- pure compute -------------------------------------------------------------------------------


def test_compute_no_events_is_all_zero() -> None:
    assert compute_late_arrivals([]) == (0, 0, 0)


def test_compute_on_time_events_are_not_late() -> None:
    # 0 min, 30 min, exactly 60 min after the span — none breach the strict >1h threshold.
    pairs = [_pair(0), _pair(30), _pair(60)]
    assert compute_late_arrivals(pairs) == (0, 0, 0)


def test_compute_counts_late_and_reports_lag_stats() -> None:
    # Three late events at 2h, 3h, 5h after the span; two on-time events ignored.
    pairs = [_pair(0), _pair(120), _pair(180), _pair(300), _pair(30)]
    events_late, median_lag, p95_lag = compute_late_arrivals(pairs)
    assert events_late == 3
    # lags (s): 7200, 10800, 18000 -> median is the middle (10800), p95 nearest-rank is the max.
    assert median_lag == 3 * 3600
    assert p95_lag == 5 * 3600


def test_compute_threshold_is_strict_greater_than() -> None:
    # Exactly at the threshold is NOT late; one second over is.
    span_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    at = (span_ts + timedelta(seconds=LATE_THRESHOLD_SECONDS), span_ts)
    over = (span_ts + timedelta(seconds=LATE_THRESHOLD_SECONDS + 1), span_ts)
    assert compute_late_arrivals([at]) == (0, 0, 0)
    assert compute_late_arrivals([over])[0] == 1


# --- store fake + endpoint ----------------------------------------------------------------------


class FakeReconciliationStore:
    """In-memory stand-in for :class:`ReconciliationStore` — no Postgres."""

    def __init__(self) -> None:
        self._runs: dict[str, list[ReconciliationRun]] = {}

    def get_latest(self, tenant_id: str) -> ReconciliationRun | None:
        runs = self._runs.get(tenant_id)
        return runs[-1] if runs else None

    def record_run(
        self,
        tenant_id: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        events_late: int,
        lag_seconds_median: int,
        lag_seconds_p95: int,
        status: str,
    ) -> ReconciliationRun:
        run = ReconciliationRun(
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            events_late=events_late,
            lag_seconds_median=lag_seconds_median,
            lag_seconds_p95=lag_seconds_p95,
            status=status,  # type: ignore[arg-type]
        )
        self._runs.setdefault(tenant_id, []).append(run)
        return run


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.reconciliation = FakeReconciliationStore()
        yield c


def test_fresh_tenant_returns_null_run(client: TestClient) -> None:
    r = client.get("/v1/tenant/reconciliation/status", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == T
    assert body["run"] is None


def test_status_reflects_latest_recorded_run(client: TestClient) -> None:
    store: FakeReconciliationStore = app.state.reconciliation
    now = datetime.now(tz=timezone.utc)
    store.record_run(
        T,
        started_at=now,
        finished_at=now,
        events_late=180,
        lag_seconds_median=15120,
        lag_seconds_p95=43200,
        status="ok",
    )
    r = client.get("/v1/tenant/reconciliation/status", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["events_late"] == 180
    assert run["lag_seconds_median"] == 15120
    assert run["lag_seconds_p95"] == 43200
    assert run["status"] == "ok"


def test_cross_tenant_isolation(client: TestClient) -> None:
    store: FakeReconciliationStore = app.state.reconciliation
    now = datetime.now(tz=timezone.utc)
    store.record_run(
        "t-a", started_at=now, finished_at=now, events_late=5,
        lag_seconds_median=7200, lag_seconds_p95=7200, status="ok",
    )
    a = client.get("/v1/tenant/reconciliation/status", headers={"X-Tenant-Id": "t-a"}).json()
    b = client.get("/v1/tenant/reconciliation/status", headers={"X-Tenant-Id": "t-b"}).json()
    assert a["run"]["events_late"] == 5
    assert b["run"] is None


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/reconciliation/status").status_code == 422


# --- orchestrator -------------------------------------------------------------------------------


class _FakeChSource:
    def __init__(self, pairs: list[tuple[datetime, datetime]], *, raises: bool = False) -> None:
        self._pairs = pairs
        self._raises = raises

    def fetch_event_span_pairs(self, tenant_id: str) -> list[tuple[datetime, datetime]]:
        if self._raises:
            raise RuntimeError("clickhouse unreachable")
        return self._pairs


def test_run_reconciliation_happy_path_records_ok() -> None:
    store = FakeReconciliationStore()
    src = _FakeChSource([_pair(120), _pair(0)])  # one late, one on-time
    run = run_reconciliation(src, store, T)  # type: ignore[arg-type]
    assert run.status == "ok"
    assert run.events_late == 1
    assert store.get_latest(T) is run


def test_run_reconciliation_records_failed_when_scan_raises() -> None:
    store = FakeReconciliationStore()
    src = _FakeChSource([], raises=True)
    run = run_reconciliation(src, store, T)  # type: ignore[arg-type]
    assert run.status == "failed"
    assert run.events_late == 0
    assert store.get_latest(T) is run
