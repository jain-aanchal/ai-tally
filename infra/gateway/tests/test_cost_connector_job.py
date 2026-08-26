# SPDX-License-Identifier: Apache-2.0
"""The daily cost-connector job: skip, run, fail, and stay idempotent (CTO-215).

No Postgres, no ClickHouse, no cloud billing API: the three config stores are in-memory fakes with
the same method shapes the real ones expose, the span sink is a dict keyed on the deterministic
synthetic span id (which is exactly what ``span_exists`` checks in production), and the billing
clients are injected.

What these tests are actually asserting is the acceptance list on the ticket:

* a configured tenant gets a run with nobody touching a script,
* an unconfigured tenant is ``JobSkipped`` and not an error,
* a failed fetch records ``failed`` and emits NO span, never a guessed number,
* re-running a day inserts nothing new,
* a run gap spanning several UTC midnights bills every missed day rather than losing them, in
  capped instalments (CTO-219, finding 1),
* a Vercel row never reads 'success' off the egress sub-run when the compute sub-run failed
  (CTO-219, finding 2),
* every configured egress provider runs, because a tenant with AWS plus Cloudflare plus Vercel is
  the normal case and their totals only sum if all of them ran,
* the ``emit_egress`` gate is respected rather than routed around.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gateway.connectors.base import ConnectorConfig, DailyCost, synthetic_span_id
from gateway.connectors.egress import EgressConfig
from gateway.connectors.vercel import VercelConfig, VercelUsageClient
from gateway.cost_connector_job import (
    JOB_NAME,
    LOOKBACK_DAYS,
    MAX_CATCHUP_DAYS,
    CostConnectorJob,
    CostConnectorRunError,
    billing_window,
    last_billed_day,
    register_cost_connector_job,
    target_day,
)
from gateway.scheduler import JobRegistry, JobSkipped

NOW = datetime(2026, 8, 25, 4, 30, 0, tzinfo=timezone.utc)
DAY = date(2026, 8, 24)  # yesterday, the day the job bills for
TENANT = "11111111-1111-1111-1111-111111111111"


class FakeSpanSink:
    """The slice of ``ClickHouseStore`` the emitter uses, over a set of span ids."""

    def __init__(self) -> None:
        self.spans: list[tuple[object, ...]] = []
        self.ids: set[tuple[str, str]] = set()
        self.closed = 0
        self.insert_error: Exception | None = None

    def span_exists(self, tenant_id: str, span_id: str) -> bool:
        return (tenant_id, span_id) in self.ids

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int:
        if self.insert_error is not None:
            raise self.insert_error
        for row in rows:
            self.spans.append(row)
        return len(rows)

    def close(self) -> None:
        self.closed += 1

    def remember(self, tenant_id: str, span_id: str) -> None:
        self.ids.add((tenant_id, span_id))

    def remember_days(self, provider: str, operation: str, days) -> None:
        """Model days that have already been billed: the span ids production would find."""
        for day in days:
            _, span_id = synthetic_span_id(TENANT, provider, operation, day)
            self.remember(TENANT, span_id)


def days_between(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        out.append(day)
        day += timedelta(days=1)
    return out


class FakeComputeStore:
    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config = config
        self.runs: list[tuple[str, str, str]] = []

    def load_config(self, tenant_id: str) -> ConnectorConfig | None:
        return self.config

    def record_run(
        self, tenant_id: str, connector_id: str, status: str, *, error_message: str | None = None
    ) -> None:
        self.runs.append((tenant_id, connector_id, status))


class FakeEgressStore:
    def __init__(self, configs: list[EgressConfig] | None = None) -> None:
        self.configs = configs or []
        self.runs: list[tuple[str, str, str]] = []

    def load_configs(self, tenant_id: str) -> list[EgressConfig]:
        return list(self.configs)

    def recorder_for(self, provider: str) -> "FakeEgressStore._Recorder":
        return FakeEgressStore._Recorder(self, provider)

    class _Recorder:
        def __init__(self, store: "FakeEgressStore", provider: str) -> None:
            self._store = store
            self._provider = provider

        def record_run(
            self,
            tenant_id: str,
            connector_id: str,
            status: str,
            *,
            error_message: str | None = None,
        ) -> None:
            self._store.runs.append((tenant_id, self._provider, status))


class FakeVercelStore:
    def __init__(self, config: VercelConfig | None = None) -> None:
        self.config = config
        self.runs: list[tuple[str, str, str]] = []

    def load_config(self, tenant_id: str) -> VercelConfig | None:
        return self.config

    def record_run(
        self, tenant_id: str, connector_id: str, status: str, *, error_message: str | None = None
    ) -> None:
        self.runs.append((tenant_id, connector_id, status))


class FakeBillingClient:
    """Returns a fixed daily total, or raises to model an upstream that is down."""

    def __init__(self, micro: int = 1_000_000, error: Exception | None = None) -> None:
        self.micro = micro
        self.error = error
        self.calls = 0

    def get_daily_costs(self, config, *, start_day: date, end_day: date) -> list[DailyCost]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [DailyCost(day=start_day, cost_micro_usd=self.micro)]


class RangeBillingClient:
    """Bills every day in the requested window, and remembers the windows it was asked for.

    A real billing API returns one row per day in the range from a single call, which is why the
    job hands a window to ``run_backfill`` instead of looping ``run()``.
    """

    def __init__(self, micro: int = 1_000_000) -> None:
        self.micro = micro
        self.windows: list[tuple[date, date]] = []

    def get_daily_costs(self, config, *, start_day: date, end_day: date) -> list[DailyCost]:
        self.windows.append((start_day, end_day))
        return [
            DailyCost(day=day, cost_micro_usd=self.micro)
            for day in days_between(start_day, end_day)
        ]


def compute_config(provider: str = "aws") -> ConnectorConfig:
    return ConnectorConfig(
        tenant_id=TENANT, cloud_provider=provider, credentials_ref="aws-default-chain"
    )


def egress_config(provider: str) -> EgressConfig:
    return EgressConfig(
        tenant_id=TENANT,
        cloud_provider=provider,
        credentials_ref=f"{provider}-ref",
        resource_id="zone-1",
        usd_per_gb=Decimal("0.09"),
    )


def build_job(
    *,
    sink: FakeSpanSink,
    compute_store: FakeComputeStore | None = None,
    egress_store: FakeEgressStore | None = None,
    vercel_store: FakeVercelStore | None = None,
    compute_client: FakeBillingClient | None = None,
    egress_clients: dict[str, FakeBillingClient] | None = None,
    vercel_payload: dict | None = None,
) -> CostConnectorJob:
    egress_clients = egress_clients or {}
    usage_client = VercelUsageClient(
        http_getter=lambda config, start, end: vercel_payload or {"items": []}
    )
    return CostConnectorJob(
        None,  # settings unused: every collaborator below is injected
        compute_store=compute_store or FakeComputeStore(),
        egress_store=egress_store or FakeEgressStore(),
        vercel_store=vercel_store or FakeVercelStore(),
        store_factory=lambda: sink,
        compute_client_factory=lambda provider: compute_client or FakeBillingClient(),
        egress_client_factory=lambda provider: egress_clients[provider],
        usage_client_factory=lambda: usage_client,
        now=lambda: NOW,
    )


def test_target_day_is_yesterday_utc():
    # Today is incomplete at every billing provider, and the emitter would freeze the partial
    # number in place because the day already has a span.
    assert target_day(NOW) == DAY


def test_unconfigured_tenant_raises_job_skipped():
    sink = FakeSpanSink()
    job = build_job(sink=sink)
    with pytest.raises(JobSkipped):
        job(TENANT)
    assert sink.spans == []


def test_paused_vercel_only_tenant_is_a_skip_not_a_run():
    # `enabled=False` is the tenant's own pause switch, so there is nothing to do rather than
    # something that failed.
    vercel = FakeVercelStore(
        VercelConfig(
            tenant_id=TENANT,
            cloud_provider="vercel",
            credentials_ref="ref",
            enabled=False,
        )
    )
    sink = FakeSpanSink()
    with pytest.raises(JobSkipped):
        build_job(sink=sink, vercel_store=vercel)(TENANT)
    assert vercel.runs == []


def test_configured_compute_tenant_runs_and_emits_one_span():
    sink = FakeSpanSink()
    compute = FakeComputeStore(compute_config("aws"))
    client = FakeBillingClient(micro=2_500_000)
    build_job(sink=sink, compute_store=compute, compute_client=client)(TENANT)
    assert client.calls == 1
    assert len(sink.spans) == 1
    # The connector owns its own run recording; the job does not shadow it.
    assert compute.runs == [(TENANT, "compute", "success")]
    assert sink.closed == 1


def test_every_configured_egress_provider_runs():
    # A tenant with AWS, Cloudflare and Vercel egress at once is normal: the composite PK allows it
    # and the three totals only sum on /cost if all three ran.
    sink = FakeSpanSink()
    egress = FakeEgressStore([egress_config(p) for p in ("aws", "cloudflare", "vercel")])
    clients = {p: FakeBillingClient() for p in ("aws", "cloudflare", "vercel")}
    build_job(sink=sink, egress_store=egress, egress_clients=clients)(TENANT)
    assert all(c.calls == 1 for c in clients.values())
    assert len(sink.spans) == 3
    assert sorted(egress.runs) == [
        (TENANT, "aws", "success"),
        (TENANT, "cloudflare", "success"),
        (TENANT, "vercel", "success"),
    ]


def test_one_failing_provider_does_not_stop_the_others():
    sink = FakeSpanSink()
    egress = FakeEgressStore([egress_config("aws"), egress_config("cloudflare")])
    clients = {
        "aws": FakeBillingClient(error=RuntimeError("cost explorer 500")),
        "cloudflare": FakeBillingClient(),
    }
    with pytest.raises(CostConnectorRunError) as excinfo:
        build_job(sink=sink, egress_store=egress, egress_clients=clients)(TENANT)
    assert "egress/aws" in str(excinfo.value)
    # The healthy provider still landed its span, and only the broken one is 'failed'.
    assert len(sink.spans) == 1
    assert sorted(egress.runs) == [(TENANT, "aws", "failed"), (TENANT, "cloudflare", "success")]


def test_failed_fetch_records_failed_and_emits_no_span():
    sink = FakeSpanSink()
    compute = FakeComputeStore(compute_config("aws"))
    client = FakeBillingClient(error=RuntimeError("boom"))
    with pytest.raises(CostConnectorRunError):
        build_job(sink=sink, compute_store=compute, compute_client=client)(TENANT)
    assert sink.spans == []  # never a guessed number
    assert compute.runs == [(TENANT, "compute", "failed")]


def test_failure_outside_the_fetch_still_lands_on_the_config_row():
    # An unsupported provider (or any client that cannot be built) never reaches the connector's own
    # error handling, so the job records it. Otherwise the tenant's row would claim the last run
    # succeeded while nothing ran at all.
    sink = FakeSpanSink()
    compute = FakeComputeStore(compute_config("azure"))
    job = CostConnectorJob(
        None,
        compute_store=compute,
        egress_store=FakeEgressStore(),
        vercel_store=FakeVercelStore(),
        store_factory=lambda: sink,
        compute_client_factory=lambda provider: (_ for _ in ()).throw(
            ValueError(f"unsupported cloud_provider {provider!r}")
        ),
        now=lambda: NOW,
    )
    with pytest.raises(CostConnectorRunError):
        job(TENANT)
    assert compute.runs == [(TENANT, "compute", "failed")]
    assert sink.spans == []
    assert sink.closed == 1  # the sink is closed even on the failure path


def test_rerunning_a_day_inserts_nothing_new():
    sink = FakeSpanSink()
    compute = FakeComputeStore(compute_config("aws"))
    client = FakeBillingClient()
    job = build_job(sink=sink, compute_store=compute, compute_client=client)
    job(TENANT)
    assert len(sink.spans) == 1
    # Model what production sees on a second fire: the day's deterministic span id is now present.
    _, span_id = synthetic_span_id(TENANT, "aws", "compute", DAY)
    sink.remember(TENANT, span_id)
    job(TENANT)
    assert len(sink.spans) == 1  # the idempotency guard held


def test_vercel_egress_gate_is_respected():
    payload = {
        "items": [
            {"date": DAY.isoformat(), "type": "function_duration", "amount": "3.00"},
            {"date": DAY.isoformat(), "type": "bandwidth", "amount": "1.00"},
        ]
    }
    # Gate off (the default): compute only, so Vercel egress stays owned by the CTO-144 path.
    sink = FakeSpanSink()
    vercel = FakeVercelStore(
        VercelConfig(tenant_id=TENANT, cloud_provider="vercel", credentials_ref="ref")
    )
    build_job(sink=sink, vercel_store=vercel, vercel_payload=payload)(TENANT)
    assert len(sink.spans) == 1

    # Gate on: compute plus egress, two spans, still one row per (provider, operation, day).
    sink_on = FakeSpanSink()
    vercel_on = FakeVercelStore(
        VercelConfig(
            tenant_id=TENANT,
            cloud_provider="vercel",
            credentials_ref="ref",
            emit_egress=True,
        )
    )
    build_job(sink=sink_on, vercel_store=vercel_on, vercel_payload=payload)(TENANT)
    assert len(sink_on.spans) == 2


def test_all_three_layers_run_for_one_tenant():
    sink = FakeSpanSink()
    compute = FakeComputeStore(compute_config("gcp"))
    egress = FakeEgressStore([egress_config("cloudflare")])
    vercel = FakeVercelStore(
        VercelConfig(tenant_id=TENANT, cloud_provider="vercel", credentials_ref="ref")
    )
    build_job(
        sink=sink,
        compute_store=compute,
        egress_store=egress,
        vercel_store=vercel,
        egress_clients={"cloudflare": FakeBillingClient()},
        vercel_payload={
            "items": [{"date": DAY.isoformat(), "type": "compute", "amount": "2.00"}]
        },
    )(TENANT)
    assert len(sink.spans) == 3
    assert compute.runs == [(TENANT, "compute", "success")]
    assert egress.runs == [(TENANT, "cloudflare", "success")]
    assert vercel.runs == [(TENANT, "compute", "success")]


def vercel_config_with_gate(emit_egress: bool) -> VercelConfig:
    return VercelConfig(
        tenant_id=TENANT,
        cloud_provider="vercel",
        credentials_ref="ref",
        emit_egress=emit_egress,
    )


# --- which days a run covers (CTO-219, finding 1) -----------------------------------------------


def test_window_is_yesterday_alone_when_nothing_has_ever_been_billed():
    # A brand new connector has no last-billed day, so there is no gap to catch up and the job must
    # not invent a backfill. Deep history stays a job for scripts/backfill_*.py.
    sink = FakeSpanSink()
    assert billing_window(sink, TENANT, [("aws", "compute")], newest=DAY) == (DAY, DAY)
    assert last_billed_day(sink, TENANT, "aws", "compute", newest=DAY) is None


def test_window_starts_the_day_after_the_last_day_actually_billed():
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=5)])
    assert last_billed_day(sink, TENANT, "aws", "compute", newest=DAY) == DAY - timedelta(days=5)
    assert billing_window(sink, TENANT, [("aws", "compute")], newest=DAY) == (
        DAY - timedelta(days=4),
        DAY,
    )


def test_window_takes_the_oldest_last_billed_day_across_a_connectors_series():
    # Vercel with the gate on emits two series onto one config row; the half that is behind sets
    # the start, and the half that is ahead just re-covers days the emitter already skips.
    sink = FakeSpanSink()
    sink.remember_days("vercel", "compute", [DAY - timedelta(days=1)])
    sink.remember_days("vercel", "egress", [DAY - timedelta(days=4)])
    assert billing_window(
        sink, TENANT, [("vercel", "compute"), ("vercel", "egress")], newest=DAY
    ) == (DAY - timedelta(days=3), DAY)


def test_window_is_capped_at_max_catchup_days():
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=10)])
    start, end = billing_window(sink, TENANT, [("aws", "compute")], newest=DAY)
    assert start == DAY - timedelta(days=9)
    assert len(days_between(start, end)) == MAX_CATCHUP_DAYS


def test_a_gap_older_than_the_lookback_window_restarts_at_yesterday():
    # Dark for longer than the search looks back: bill yesterday and start a fresh chain, rather
    # than probing unboundedly or trying to bill a quarter of history in one invocation.
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=LOOKBACK_DAYS + 5)])
    assert billing_window(sink, TENANT, [("aws", "compute")], newest=DAY) == (DAY, DAY)


def test_a_gap_over_several_midnights_bills_every_missed_day():
    # The CTO-215 bug: a run gap spanning two UTC midnights skipped a day forever, because the day
    # was never asked for again and span_exists means a later run cannot backfill it.
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=5)])
    compute = FakeComputeStore(compute_config("aws"))
    client = RangeBillingClient()
    build_job(sink=sink, compute_store=compute, compute_client=client)(TENANT)
    # One billing-API call for the whole window, not one per day.
    assert client.windows == [(DAY - timedelta(days=4), DAY)]
    assert len(sink.spans) == 5
    assert compute.runs == [(TENANT, "compute", "success")]


def test_a_long_gap_is_capped_and_the_remainder_is_taken_on_the_next_run():
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=10)])
    compute = FakeComputeStore(compute_config("aws"))
    client = RangeBillingClient()
    job = build_job(sink=sink, compute_store=compute, compute_client=client)

    job(TENANT)
    first_start, first_end = client.windows[0]
    assert first_start == DAY - timedelta(days=9)
    assert len(days_between(first_start, first_end)) == MAX_CATCHUP_DAYS
    assert len(sink.spans) == MAX_CATCHUP_DAYS

    # Production now sees those days as billed; the next run picks up from the new last-billed day
    # rather than losing the remainder.
    sink.remember_days("aws", "compute", days_between(first_start, first_end))
    job(TENANT)
    assert client.windows[1] == (first_end + timedelta(days=1), DAY)
    assert len(sink.spans) == 10  # every day of the gap billed exactly once

    # And a third run, fully caught up, asks only for yesterday and inserts nothing new.
    sink.remember_days("aws", "compute", days_between(first_end + timedelta(days=1), DAY))
    job(TENANT)
    assert client.windows[2] == (DAY, DAY)
    assert len(sink.spans) == 10


def test_each_connector_gets_its_own_window():
    # An egress provider connected yesterday must not be dragged over compute's gap, and vice
    # versa: each series has its own last-billed day.
    sink = FakeSpanSink()
    sink.remember_days("aws", "compute", [DAY - timedelta(days=3)])
    sink.remember_days("cloudflare", "egress", [DAY - timedelta(days=1)])
    compute_client = RangeBillingClient()
    egress_client = RangeBillingClient()
    build_job(
        sink=sink,
        compute_store=FakeComputeStore(compute_config("aws")),
        egress_store=FakeEgressStore([egress_config("cloudflare")]),
        compute_client=compute_client,
        egress_clients={"cloudflare": egress_client},
    )(TENANT)
    assert compute_client.windows == [(DAY - timedelta(days=2), DAY)]
    assert egress_client.windows == [(DAY, DAY)]


def test_catch_up_stays_idempotent_over_days_already_billed():
    # A widened window is the same guarantee applied to more days: the emitter skips any day that
    # already has a span. Vercel with the gate on is where this actually bites, because its egress
    # half can be behind its compute half, so the shared window re-covers days compute already has.
    days = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
    payload = {
        "items": [
            item
            for day in days
            for item in (
                {"date": day.isoformat(), "type": "compute", "amount": "2.00"},
                {"date": day.isoformat(), "type": "bandwidth", "amount": "1.00"},
            )
        ]
    }
    sink = FakeSpanSink()
    sink.remember_days("vercel", "compute", [DAY - timedelta(days=1)])
    sink.remember_days("vercel", "egress", [DAY - timedelta(days=3)])
    vercel = FakeVercelStore(vercel_config_with_gate(True))
    build_job(sink=sink, vercel_store=vercel, vercel_payload=payload)(TENANT)
    # Six day-slots in the window across the two series, one of which was already billed.
    assert len(sink.spans) == 5


# --- one shared row, one status (CTO-219, finding 2) --------------------------------------------


def test_failed_compute_sub_run_does_not_leave_the_vercel_row_reading_success():
    # The compute sub-run fails and the egress sub-run then succeeds onto the SAME
    # tenant_vercel_config row. Before CTO-219 the row read 'success' while no compute span had
    # been emitted: a tenant looking at a healthy connector that was not producing data.
    calls: list[int] = []

    def flaky(config, start, end):
        calls.append(1)
        if len(calls) == 1:  # the compute half fetches first
            raise RuntimeError("vercel usage 503")
        return {"items": [{"date": DAY.isoformat(), "type": "bandwidth", "amount": "1.00"}]}

    sink = FakeSpanSink()
    vercel = FakeVercelStore(vercel_config_with_gate(True))
    job = CostConnectorJob(
        None,
        compute_store=FakeComputeStore(),
        egress_store=FakeEgressStore(),
        vercel_store=vercel,
        store_factory=lambda: sink,
        usage_client_factory=lambda: VercelUsageClient(http_getter=flaky),
        now=lambda: NOW,
    )
    with pytest.raises(CostConnectorRunError):
        job(TENANT)

    statuses = [status for _, _, status in vercel.runs]
    assert "success" not in statuses
    assert statuses == ["failed"]  # stamped once, with the worst outcome, not the last one


def test_failed_egress_sub_run_also_lands_on_the_vercel_row():
    # The mirror image: compute succeeds, egress fails. The row must still not read success.
    calls: list[int] = []

    def flaky(config, start, end):
        calls.append(1)
        if len(calls) == 1:
            return {"items": [{"date": DAY.isoformat(), "type": "compute", "amount": "2.00"}]}
        raise RuntimeError("vercel usage 503")

    sink = FakeSpanSink()
    vercel = FakeVercelStore(vercel_config_with_gate(True))
    job = CostConnectorJob(
        None,
        compute_store=FakeComputeStore(),
        egress_store=FakeEgressStore(),
        vercel_store=vercel,
        store_factory=lambda: sink,
        usage_client_factory=lambda: VercelUsageClient(http_getter=flaky),
        now=lambda: NOW,
    )
    with pytest.raises(CostConnectorRunError):
        job(TENANT)
    assert [status for _, _, status in vercel.runs] == ["failed"]
    assert len(sink.spans) == 1  # the compute half still landed


def test_gate_off_records_exactly_one_status_as_before():
    # A tenant with emit_egress=false has only a compute sub-run, and that case is unchanged: one
    # record_run, with the connector's own outcome.
    sink = FakeSpanSink()
    vercel = FakeVercelStore(vercel_config_with_gate(False))
    build_job(
        sink=sink,
        vercel_store=vercel,
        vercel_payload={
            "items": [{"date": DAY.isoformat(), "type": "compute", "amount": "2.00"}]
        },
    )(TENANT)
    assert vercel.runs == [(TENANT, "compute", "success")]


def test_both_vercel_sub_runs_succeeding_still_records_success_once():
    payload = {
        "items": [
            {"date": DAY.isoformat(), "type": "compute", "amount": "2.00"},
            {"date": DAY.isoformat(), "type": "bandwidth", "amount": "1.00"},
        ]
    }
    sink = FakeSpanSink()
    vercel = FakeVercelStore(vercel_config_with_gate(True))
    build_job(sink=sink, vercel_store=vercel, vercel_payload=payload)(TENANT)
    assert vercel.runs == [(TENANT, "compute", "success")]
    assert len(sink.spans) == 2


def test_registration_uses_the_stable_job_name_and_a_daily_cadence():
    registry = JobRegistry()
    job = register_cost_connector_job(registry, None, job=build_job(sink=FakeSpanSink()))
    assert job.name == JOB_NAME
    assert job.interval_s == 86400.0
    assert len(registry) == 1
