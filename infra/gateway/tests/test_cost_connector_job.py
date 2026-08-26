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
* every configured egress provider runs, because a tenant with AWS plus Cloudflare plus Vercel is
  the normal case and their totals only sum if all of them ran,
* the ``emit_egress`` gate is respected rather than routed around.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from gateway.connectors.base import ConnectorConfig, DailyCost, synthetic_span_id
from gateway.connectors.egress import EgressConfig
from gateway.connectors.vercel import VercelConfig, VercelUsageClient
from gateway.cost_connector_job import (
    JOB_NAME,
    CostConnectorJob,
    CostConnectorRunError,
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


def test_registration_uses_the_stable_job_name_and_a_daily_cadence():
    registry = JobRegistry()
    job = register_cost_connector_job(registry, None, job=build_job(sink=FakeSpanSink()))
    assert job.name == JOB_NAME
    assert job.interval_s == 86400.0
    assert len(registry) == 1
