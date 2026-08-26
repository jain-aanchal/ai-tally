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
from datetime import datetime, timedelta, timezone

import pytest

from gateway.ingest_cursors import INITIAL_LOOKBACK_S, next_cursor, overlap_for
from gateway.integration_workers import (
    DEFAULT_CONNECTOR_TIMEOUT_S,
    UrllibHttpClient,
    build_http_client,
    timeout_for,
)
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
    FakeCursorStore,
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
    cursors: FakeCursorStore | None = None,
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
        cursors=cursors if cursors is not None else FakeCursorStore(),  # type: ignore[arg-type]
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

    def _partial(tenant_id, secret, token, window):  # noqa: ANN001, ANN202
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
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), span_slack_days=0)
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), max_rows_to_read=0)
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), max_memory_bytes=0)
    with pytest.raises(ValueError):
        ClickHouseLateArrivalSource(FakeCHStore(), max_execution_s=0)


# --- CTO-219 finding 3: the reconciler scan is BOUNDED, not merely limited -----------------------


class _RecordingCHClient:
    """Captures the SQL, parameters and settings one query was issued with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query(self, sql: str, parameters=None, settings=None):  # noqa: ANN001, ANN202
        self.calls.append({"sql": sql, "parameters": parameters, "settings": settings})

        class _R:
            result_rows: list[tuple[datetime, datetime]] = []

        return _R()


class _RecordingCHStore:
    def __init__(self) -> None:
        self.client = _RecordingCHClient()


def test_reconciler_span_side_is_bounded_by_the_event_window_not_by_the_clock() -> None:
    """The finding: `LIMIT 50000` caps rows RETURNED; the ASOF join materialises spans first.

    So the span side must be narrowed by the events actually selected. The scalar subqueries against
    the capped event set are what ClickHouse folds into literal `Timestamp` bounds, which is what
    prunes parts on `otel_spans` (PARTITION BY toDate(Timestamp)).
    """
    store = _RecordingCHStore()
    source = ClickHouseLateArrivalSource(store)
    source.fetch_event_span_pairs(T)

    sql = " ".join(str(store.client.calls[0]["sql"]).split())
    assert "ASOF INNER JOIN" in sql
    # The span side is bounded ABOVE by the newest event and BELOW by the oldest, both read from the
    # capped event set rather than from now().
    assert "Timestamp <= (SELECT max(OccurredAt) FROM capped_events)" in sql
    assert "(SELECT min(OccurredAt) FROM capped_events)" in sql
    # The fixed lookback survives only as a FLOOR under the derived bound, never as the bound.
    assert "Timestamp >= subtractDays(now64(9), {span_days:UInt32})" in sql
    # Both sides stay tenant-scoped: an untenanted scan is a whole-cluster scan (see otel_spans.sql).
    assert sql.count("TenantId = {tenant:String}") == 2
    # And it is still not sampled: sampling the span side would make the ASOF join match an EARLIER
    # span and over-report lateness, i.e. change the answer rather than bound the work.
    assert "SAMPLE" not in sql


def test_reconciler_span_window_is_no_wider_than_the_event_window_plus_slack() -> None:
    store = _RecordingCHStore()
    source = ClickHouseLateArrivalSource(store, lookback_days=7, span_slack_days=1)
    source.fetch_event_span_pairs(T)
    params = store.client.calls[0]["parameters"]
    assert isinstance(params, dict)
    # The old query searched spans over lookback_days * 2 = 14 days, unconditionally. The floor is
    # now the event window plus one day of slack, and the derived bound is usually far tighter.
    assert params["days"] == 7
    assert params["span_days"] == 8
    assert params["span_slack_days"] == 1
    assert params["cap"] == 50_000


def test_reconciler_query_carries_fail_fast_server_side_ceilings() -> None:
    """A runaway pass must fail honestly and fast, never take ClickHouse (and the gateway) with it."""
    store = _RecordingCHStore()
    source = ClickHouseLateArrivalSource(store)
    source.fetch_event_span_pairs(T)
    settings = store.client.calls[0]["settings"]
    assert isinstance(settings, dict)
    # Rows SCANNED, which is the bound the LIMIT never was.
    assert settings["max_rows_to_read"] > 0
    assert settings["max_memory_usage"] > 0
    assert settings["max_execution_time"] > 0
    # `throw`, never `break`: a truncated result looks exactly like a complete one, and the card
    # would then show a confidently wrong lateness figure.
    assert settings["read_overflow_mode"] == "throw"
    assert settings["timeout_overflow_mode"] == "throw"
    assert source.query_settings == settings


def test_a_reconciler_that_blows_its_ceiling_records_failed_and_never_a_number() -> None:
    """MEMORY_LIMIT_EXCEEDED is the exact failure this finding is about. It must be honest."""
    from gateway.reconciliation import run_reconciliation

    store = FakeReconciliationStore()
    source = FakeLateArrivals(
        [], error=RuntimeError("Memory limit (for query) exceeded: would use 9.31 GiB")
    )
    run = run_reconciliation(source, store, T)  # type: ignore[arg-type]
    assert run.status == "failed"
    # Zeroed metrics, not a guessed lateness. The card reads "ran, but errored".
    assert (run.events_late, run.lag_seconds_median, run.lag_seconds_p95) == (0, 0, 0)


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
        cursors=FakeCursorStore(),  # type: ignore[arg-type]
    )
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    worker = job.fn.__closure__[0].cell_contents  # type: ignore[index, union-attr]

    def _ingest(tenant_id, secret, token, window):  # noqa: ANN001, ANN202
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
        cursors=FakeCursorStore(),  # type: ignore[arg-type]
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


# --- CTO-219 finding 5: HTTP timeouts are per integration ----------------------------------------


def test_timeouts_are_per_integration_and_pendo_gets_more_room_than_segment() -> None:
    """One 30s global default contradicted this module's own Pendo cadence argument.

    That argument leans on Pendo's DOCUMENTED 5-minute aggregation timeout to justify the 30-minute
    cadence. A 30s client timeout therefore killed every slow Pendo tenant on every cycle, recorded
    `failed`, and backed the connector off to the 6h cap: dead for exactly the tenants with the most
    data.
    """
    seg = build_http_client("segment")
    hub = build_http_client("hubspot")
    pen = build_http_client("pendo")

    assert pen.timeout_s > seg.timeout_s
    assert pen.timeout_s > hub.timeout_s
    # Pendo has to outlast Pendo's own documented 300s query timeout, or we cannot tell a slow
    # aggregation from a broken one.
    assert pen.timeout_s > 300.0
    # And Segment and HubSpot stay TIGHT. Raising the global default instead would hold a worker
    # thread for five minutes on a hung Segment connection, and those threads are the gateway's.
    assert seg.timeout_s <= 30.0
    assert hub.timeout_s <= 30.0
    assert timeout_for("segment") == seg.timeout_s
    # An unsized connector gets the tight default, not the generous one.
    assert timeout_for("not-a-connector") == DEFAULT_CONNECTOR_TIMEOUT_S <= 30.0


def test_each_registered_worker_gets_its_own_client_sized_for_its_provider() -> None:
    """One shared client would put all three providers back on one number, which is the bug."""
    reg = JobRegistry()
    register_worker_jobs(
        reg,
        object(),
        store=FakeCHStore(),  # type: ignore[arg-type]
        hmac_registry=_HmacRegistryStub(),  # type: ignore[arg-type]
        secrets=FakeSecretStore(make_secret("segment")),  # type: ignore[arg-type]
        resolver=FakeResolver({"ref-1": "tok"}),  # type: ignore[arg-type]
        integrations=FakeIntegrations(),  # type: ignore[arg-type]
        reconciliation_store=FakeReconciliationStore(),  # type: ignore[arg-type]
        cursors=FakeCursorStore(),  # type: ignore[arg-type]
    )
    timeouts = {}
    for connector in ("segment", "hubspot", "pendo"):
        job = reg.get(INGEST_JOB_NAMES[connector])
        assert job is not None
        worker = job.fn.__closure__[0].cell_contents  # type: ignore[index, union-attr]
        timeouts[connector] = worker._http.timeout_s
    assert timeouts["pendo"] > timeouts["segment"]
    assert timeouts == {c: timeout_for(c) for c in timeouts}


def test_a_client_with_no_timeout_is_a_construction_error() -> None:
    """No global default to fall back into. A caller has to say whose budget it is spending."""
    with pytest.raises(TypeError):
        UrllibHttpClient()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        UrllibHttpClient(timeout_s=0)


# --- CTO-219 finding 6: ingest is incremental ----------------------------------------------------
#
# Be accurate about what this fixes. Re-pulling did NOT corrupt data: BusinessEventId is the
# provider's stable id and business_events is ReplacingMergeTree(IngestedAt) ORDER BY
# (TenantId, BusinessEventId), so re-insertion collapses by design. What it cost was write
# amplification and a transient pre-merge double count on the reads that lack FINAL.


def _segment_worker(cursors: FakeCursorStore, http: FakeHttp) -> object:
    reg = JobRegistry()
    register_worker_jobs(
        reg,
        object(),
        store=FakeCHStore(),  # type: ignore[arg-type]
        hmac_registry=_HmacRegistryStub(),  # type: ignore[arg-type]
        secrets=FakeSecretStore(make_secret("segment")),  # type: ignore[arg-type]
        resolver=FakeResolver({"ref-1": "tok"}),  # type: ignore[arg-type]
        http=http,
        integrations=FakeIntegrations(),  # type: ignore[arg-type]
        reconciliation_store=FakeReconciliationStore(),  # type: ignore[arg-type]
        cursors=cursors,  # type: ignore[arg-type]
    )
    job = reg.get(INGEST_JOB_NAMES["segment"])
    assert job is not None
    return job.fn


def test_a_first_run_with_no_cursor_works_and_is_bounded() -> None:
    """No cursor must mean a bounded initial window, never "every event the provider ever had"."""
    cursors = FakeCursorStore()
    http = FakeHttp(payload={"events": []})
    run = _segment_worker(cursors, http)
    before = datetime.now(tz=timezone.utc)
    run(T)  # type: ignore[operator]

    params = http.calls[0]["params"]
    assert params is not None, "a first cycle must still ask for a WINDOW, not the default payload"
    since = datetime.fromisoformat(str(params["start"]))
    end = datetime.fromisoformat(str(params["end"]))
    span_s = (end - since).total_seconds()
    assert span_s == pytest.approx(INITIAL_LOOKBACK_S, abs=5.0)
    # Bounded means bounded: not the epoch, and not open-ended.
    assert since > before - timedelta(seconds=INITIAL_LOOKBACK_S + 5)
    assert end <= datetime.now(tz=timezone.utc)


def test_a_second_cycle_asks_only_for_what_is_new() -> None:
    """The whole point: a 15-minute cadence stops re-sending the provider's whole payload 96x/day."""
    cursors = FakeCursorStore()
    http = FakeHttp(payload={"events": []})
    run = _segment_worker(cursors, http)

    run(T)  # type: ignore[operator]
    run(T)  # type: ignore[operator]

    first_end = datetime.fromisoformat(str(http.calls[0]["params"]["end"]))  # type: ignore[index]
    second_since = datetime.fromisoformat(str(http.calls[1]["params"]["start"]))  # type: ignore[index]
    second_end = datetime.fromisoformat(str(http.calls[1]["params"]["end"]))  # type: ignore[index]

    # The second window starts where the first ended, less Segment's overlap. It is therefore two
    # orders of magnitude narrower than the first, which is the write amplification that goes away.
    assert (second_end - second_since).total_seconds() <= overlap_for("segment") + 5
    assert (second_end - second_since).total_seconds() < INITIAL_LOOKBACK_S / 100
    assert (first_end - second_since).total_seconds() == pytest.approx(
        overlap_for("segment"), abs=5.0
    )


def test_the_overlap_is_per_provider_and_covers_pendos_aggregation_latency() -> None:
    """Pendo documents ~5 minutes of aggregation latency, so its overlap has to exceed that.

    Without it, a first-use Pendo had not aggregated when we asked is not merely re-fetched later,
    it is missed forever, because the next cycle starts after it.
    """
    assert overlap_for("pendo") > 300.0
    assert overlap_for("segment") < overlap_for("pendo")
    assert overlap_for("hubspot") < overlap_for("pendo")
    end = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert next_cursor("pendo", end) == end - timedelta(seconds=overlap_for("pendo"))


def test_a_failed_cycle_does_not_advance_the_cursor() -> None:
    """The window was not handled, so the next cycle must ask for it again or the events are lost."""
    cursors = FakeCursorStore()
    http = FakeHttp(error=RuntimeError("HTTP 503 from api.segment.io"))
    run = _segment_worker(cursors, http)
    with pytest.raises(Exception):
        run(T)  # type: ignore[operator]
    assert cursors.advances == []
    assert cursors.get(T, "segment") is None


def test_a_partial_cycle_does_advance_the_cursor() -> None:
    """Partial means records failed to MAP, which is deterministic. Holding back never progresses."""
    cursors = FakeCursorStore()
    # A non-dict item in the payload is exactly the "one bad record" path that yields `partial`.
    http = FakeHttp(payload={"events": ["not-an-object"]})
    run = _segment_worker(cursors, http)
    run(T)  # type: ignore[operator]
    assert len(cursors.advances) == 1
    assert cursors.get(T, "segment") is not None


def test_a_cursor_store_outage_widens_the_window_rather_than_dropping_events() -> None:
    """A control-plane blip should cost bandwidth (which is idempotent), never data."""
    cursors = FakeCursorStore()
    http = FakeHttp(payload={"events": []})
    run = _segment_worker(cursors, http)
    cursors.raise_on_get = True
    cursors.raise_on_advance = True
    run(T)  # type: ignore[operator]  # no exception: the cycle still succeeds

    params = http.calls[0]["params"]
    since = datetime.fromisoformat(str(params["start"]))  # type: ignore[index]
    end = datetime.fromisoformat(str(params["end"]))  # type: ignore[index]
    assert (end - since).total_seconds() == pytest.approx(INITIAL_LOOKBACK_S, abs=5.0)


def test_the_cursor_never_rewinds() -> None:
    """Monotonic, so a skewed clock or a replayed older window cannot undo the fix."""
    cursors = FakeCursorStore()
    later = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    cursors.advance(T, "segment", later)
    cursors.advance(T, "segment", later - timedelta(hours=6))
    assert cursors.get(T, "segment") == later


def test_a_cursor_from_the_future_clamps_to_an_empty_window_not_an_inverted_one() -> None:
    now = datetime.now(tz=timezone.utc)
    cursors = FakeCursorStore({(T, "segment"): now + timedelta(days=1)})
    http = FakeHttp(payload={"events": []})
    run = _segment_worker(cursors, http)
    run(T)  # type: ignore[operator]
    params = http.calls[0]["params"]
    since = datetime.fromisoformat(str(params["start"]))  # type: ignore[index]
    end = datetime.fromisoformat(str(params["end"]))  # type: ignore[index]
    assert since <= end
