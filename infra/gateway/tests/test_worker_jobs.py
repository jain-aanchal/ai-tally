# SPDX-License-Identifier: Apache-2.0
"""Scheduled ingest + reconciliation jobs (CTO-216).

These pin the seam between two things that already work on their own: the workers' honest
``success`` / ``partial`` / ``failed`` / ``skipped`` cycle status, and the scheduler's
``success`` / ``skipped`` / ``failed`` run history. Specifically:

* an unconfigured tenant raises ``JobSkipped`` (settles the window, never looks like fresh data),
* ``partial`` maps to scheduler success rather than failure, and ``record_run`` still records it,
* a failed cycle raises so the scheduler records ``failed`` with a scrubbed error,
* one tenant's failure does not stop another tenant's run, driven through a real ``tick_once``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from gateway.reconciliation import ClickHouseLateArrivalSource, ReconciliationRun
from gateway.scheduler import JobRegistry, JobSkipped, Scheduler
from gateway.worker_jobs import (
    INGEST_INTERVAL_S,
    INGEST_JOB_NAMES,
    PENDO_INGEST_INTERVAL_S,
    RECONCILIATION_INTERVAL_S,
    RECONCILIATION_JOB_NAME,
    register_worker_jobs,
)

from _worker_fakes import (
    FakeCHStore,
    FakeHttp,
    FakeIntegrations,
    FakeResolver,
    FakeSecretStore,
    make_secret,
)

T = "t-acme"
OTHER = "t-globex"


class FakeReconciliationStore:
    """Captures reconciliation ``record_run`` calls and hands back the row it would have written."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_run(self, tenant_id: str, **kwargs: object) -> ReconciliationRun:
        self.calls.append({"tenant_id": tenant_id, **kwargs})
        return ReconciliationRun(
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            events_late=int(kwargs["events_late"]),  # type: ignore[arg-type]
            lag_seconds_median=int(kwargs["lag_seconds_median"]),  # type: ignore[arg-type]
            lag_seconds_p95=int(kwargs["lag_seconds_p95"]),  # type: ignore[arg-type]
            status=kwargs["status"],  # type: ignore[arg-type]
        )


class FakeLateArrivals:
    """A ``_ClickHouseEventSource`` that returns canned pairs or raises like a broken CH scan."""

    def __init__(self, pairs: list[tuple[datetime, datetime]], error: Exception | None = None):
        self._pairs = pairs
        self._error = error

    def fetch_event_span_pairs(self, tenant_id: str) -> list[tuple[datetime, datetime]]:
        if self._error is not None:
            raise self._error
        return self._pairs


class _HmacRegistryStub:
    """Enough of ``HmacKeyRegistry`` for the workers' ``build_hasher``."""

    def provision(self, tenant_id: str) -> None:
        return None

    def hash(self, tenant_id: str, value: str):  # noqa: ANN201 - shape matches HmacKeyRegistry
        class _H:
            value = "f" * 64

        return _H()


def _register(
    *,
    secret_for: str | None = "segment",
    http: FakeHttp | None = None,
    integrations: FakeIntegrations | None = None,
    registry: JobRegistry | None = None,
) -> tuple[JobRegistry, FakeIntegrations]:
    reg = registry if registry is not None else JobRegistry()
    ints = integrations if integrations is not None else FakeIntegrations()
    secret = make_secret(secret_for) if secret_for else None
    register_worker_jobs(
        reg,
        object(),  # settings: never touched, every collaborator below is injected
        store=FakeCHStore(),  # type: ignore[arg-type]
        hmac_registry=_HmacRegistryStub(),  # type: ignore[arg-type]
        secrets=FakeSecretStore(secret),  # type: ignore[arg-type]
        resolver=FakeResolver({"ref-1": "tok"}),  # type: ignore[arg-type]
        http=http if http is not None else FakeHttp(payload={"events": []}),
        integrations=ints,  # type: ignore[arg-type]
        reconciliation_store=FakeReconciliationStore(),  # type: ignore[arg-type]
    )
    return reg, ints


# --- registration --------------------------------------------------------------------------------


def test_registers_three_ingest_jobs_and_the_reconciler() -> None:
    reg, _ = _register()
    names = [job.name for job in reg.jobs()]
    assert names == [
        INGEST_JOB_NAMES["segment"],
        INGEST_JOB_NAMES["hubspot"],
        INGEST_JOB_NAMES["pendo"],
        RECONCILIATION_JOB_NAME,
    ]


def test_cadences_are_the_documented_ones() -> None:
    reg, _ = _register()
    by_name = {job.name: job.interval_s for job in reg.jobs()}
    assert by_name[INGEST_JOB_NAMES["segment"]] == INGEST_INTERVAL_S == 900.0
    assert by_name[INGEST_JOB_NAMES["hubspot"]] == INGEST_INTERVAL_S
    # Pendo is deliberately slower: it publishes no rate limits at all.
    assert by_name[INGEST_JOB_NAMES["pendo"]] == PENDO_INGEST_INTERVAL_S == 1800.0
    assert by_name[RECONCILIATION_JOB_NAME] == RECONCILIATION_INTERVAL_S == 3600.0


def test_registering_twice_on_one_registry_is_an_error_not_a_silent_overwrite() -> None:
    reg, _ = _register()
    with pytest.raises(ValueError):
        _register(registry=reg)


# --- status mapping ------------------------------------------------------------------------------


def test_unconfigured_tenant_raises_job_skipped_and_records_nothing() -> None:
    reg, ints = _register(secret_for=None)
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    with pytest.raises(JobSkipped):
        job.fn(T)
    # An unconnected integration is not a run: run_cycle deliberately writes no row for it.
    assert ints.calls == []


def test_successful_cycle_returns_and_records_through_the_existing_path() -> None:
    payload = {
        "events": [
            {
                "type": "track",
                "messageId": "m-1",
                "event": "signup",
                "userId": "u-1",
                "timestamp": "2026-06-01T00:00:00Z",
            }
        ]
    }
    reg, ints = _register(http=FakeHttp(payload=payload))
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    assert job.fn(T) is None  # no exception: scheduler records `success`
    assert [c["status"] for c in ints.calls] == ["success"]
    assert ints.calls[0]["event_count"] == 1


def test_failed_cycle_raises_so_the_scheduler_records_failed() -> None:
    reg, ints = _register(http=FakeHttp(error=RuntimeError("HTTP 503 from api.segment.io")))
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    with pytest.raises(Exception) as excinfo:
        job.fn(T)
    assert not isinstance(excinfo.value, JobSkipped)
    # The existing status path still recorded the failure itself; the raise is only for the scheduler.
    assert [c["status"] for c in ints.calls] == ["failed"]


def test_partial_cycle_is_scheduler_success_but_still_recorded_as_partial() -> None:
    """The one mapping with no scheduler equivalent. See worker_jobs' module docstring."""
    reg, ints = _register()
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    worker = job.fn.__closure__[0].cell_contents  # type: ignore[index, union-attr]

    from gateway.integration_workers import IngestOutcome

    def _partial(tenant_id, secret, token):  # noqa: ANN001, ANN202
        return IngestOutcome(event_count=3, partial=True, error_message="1 of 4 pages unreadable")

    worker._ingest = _partial  # type: ignore[method-assign]
    assert job.fn(T) is None  # partial does NOT raise: the window is genuinely satisfied
    assert [c["status"] for c in ints.calls] == ["partial"]
    assert ints.calls[0]["event_count"] == 3


# --- error scrubbing -----------------------------------------------------------------------------


def test_recorded_error_is_scrubbed_of_pii() -> None:
    reg, ints = _register(http=FakeHttp(error=RuntimeError("rejected for alice@example.com")))
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    with pytest.raises(Exception):
        job.fn(T)
    recorded = str(ints.calls[0]["error_message"])
    assert "alice@example.com" not in recorded
    assert "redacted" in recorded


# --- reconciliation ------------------------------------------------------------------------------


def test_reconciliation_records_a_run_and_does_not_skip_any_tenant() -> None:
    store = FakeReconciliationStore()
    span_ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    late = span_ts.replace(hour=18)
    from gateway.worker_jobs import _reconciliation_job_body

    body = _reconciliation_job_body(FakeLateArrivals([(late, span_ts)]), store)  # type: ignore[arg-type]
    assert body(T) is None
    assert store.calls[0]["status"] == "ok"
    assert store.calls[0]["events_late"] == 1


def test_reconciliation_scan_failure_raises_so_backoff_applies() -> None:
    store = FakeReconciliationStore()
    from gateway.worker_jobs import ReconciliationFailed, _reconciliation_job_body

    body = _reconciliation_job_body(  # type: ignore[arg-type]
        FakeLateArrivals([], error=RuntimeError("clickhouse unreachable")), store
    )
    with pytest.raises(ReconciliationFailed):
        body(T)
    # run_reconciliation still recorded its own failed row: the raise is only the scheduler signal.
    assert store.calls[0]["status"] == "failed"


def test_late_arrival_source_rejects_nonsense_bounds() -> None:
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), lookback_days=0)
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), event_cap=0)


# --- failure isolation, through a real tick ------------------------------------------------------


class _MemoryRunStore:
    """In-memory ``RunStore``: enough for one tick, and records every row the scheduler writes."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str | None]] = []

    def get_state(self, job_name: str, tenant_id: str):  # noqa: ANN201
        from gateway.scheduler import JobState

        return JobState()  # never run: everything is due

    def record_run(  # noqa: ANN001, ANN201
        self,
        job_name,
        tenant_id,
        status,
        *,
        started_at,
        finished_at,
        error_message=None,
    ):
        self.rows.append((job_name, tenant_id, status, error_message))


def test_one_tenants_failure_does_not_stop_another_tenants_run() -> None:
    # The worker resolves the tenant from run_cycle's argument, not the URL, so the split is driven
    # through the worker's own _ingest: tenant T raises, tenant OTHER succeeds.
    reg = JobRegistry()
    ints = FakeIntegrations()
    register_worker_jobs(
        reg,
        object(),
        store=FakeCHStore(),  # type: ignore[arg-type]
        hmac_registry=_HmacRegistryStub(),  # type: ignore[arg-type]
        secrets=FakeSecretStore(make_secret("segment")),  # type: ignore[arg-type]
        resolver=FakeResolver({"ref-1": "tok"}),  # type: ignore[arg-type]
        http=FakeHttp(payload={"events": []}),
        integrations=ints,  # type: ignore[arg-type]
        reconciliation_store=FakeReconciliationStore(),  # type: ignore[arg-type]
    )
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    worker = job.fn.__closure__[0].cell_contents  # type: ignore[index, union-attr]

    def _ingest(tenant_id, secret, token):  # noqa: ANN001, ANN202
        from gateway.integration_workers import IngestOutcome

        if tenant_id == T:
            raise RuntimeError("segment 500")
        return IngestOutcome(event_count=2)

    worker._ingest = _ingest  # type: ignore[method-assign]

    single = JobRegistry()
    single.register(job.name, job.interval_s, job.fn)
    runs = _MemoryRunStore()
    scheduler = Scheduler(single, runs, lambda: [T, OTHER], tick_interval_s=1.0)
    result = asyncio.run(scheduler.tick_once())

    assert result.ran == 2
    assert result.failed == 1
    assert result.succeeded == 1
    by_tenant = {tenant: status for _, tenant, status, _ in runs.rows}
    assert by_tenant == {T: "failed", OTHER: "success"}


def test_skip_and_failure_coexist_across_tenants_in_one_tick() -> None:
    reg = JobRegistry()
    ints = FakeIntegrations()

    class _SecretForOneTenant:
        def get(self, tenant_id: str, connector_id: str):  # noqa: ANN201
            return make_secret("segment") if tenant_id == T else None

    register_worker_jobs(
        reg,
        object(),
        store=FakeCHStore(),  # type: ignore[arg-type]
        hmac_registry=_HmacRegistryStub(),  # type: ignore[arg-type]
        secrets=_SecretForOneTenant(),  # type: ignore[arg-type]
        resolver=FakeResolver({"ref-1": "tok"}),  # type: ignore[arg-type]
        http=FakeHttp(payload={"events": []}),
        integrations=ints,  # type: ignore[arg-type]
        reconciliation_store=FakeReconciliationStore(),  # type: ignore[arg-type]
    )
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    single = JobRegistry()
    single.register(job.name, job.interval_s, job.fn)
    runs = _MemoryRunStore()
    scheduler = Scheduler(single, runs, lambda: [T, OTHER], tick_interval_s=1.0)
    result = asyncio.run(scheduler.tick_once())

    assert result.succeeded == 1
    assert result.skipped_by_job == 1
    by_tenant = {tenant: status for _, tenant, status, _ in runs.rows}
    assert by_tenant == {T: "success", OTHER: "skipped"}
