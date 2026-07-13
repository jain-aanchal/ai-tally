"""Egress cost-layer connector (CTO-144) — offline tests, NO live cloud calls.

Pins the contract the /cost Egress column depends on:

  * Vercel / Cloudflare / AWS fixtures parse to the correct per-day micro-USD totals (Cloudflare's
    bytes-out priced at the tenant's usd_per_gb rate; a missing rate fails soft, never a guess).
  * The connector lands ONE synthetic span per provider per day with GenAiOperation='egress' and the
    day's total as EstimatedCost (cost set directly — no catalog enrichment).
  * MULTIPLE providers for one tenant sum with NO double-counting — distinct provider ⇒ distinct
    synthetic span id ⇒ three independent spans for the same day.
  * Backfill is idempotent; a failed fetch records 'failed' and emits NO span.

Reuses the CTO-143 base verbatim: EgressCostConnector only wires the fetch. Everything
provider-specific is behind an injected fake BillingClient / fake store — no test touches boto3,
requests, Cloudflare, ClickHouse, or Postgres.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gateway.connectors.base import DailyCost, synthetic_span_id
from gateway.connectors.egress import (
    EgressConfig,
    EgressCostConnector,
    parse_aws_cost_response,
    parse_cloudflare_bytes,
    parse_vercel_usage,
)
from gateway.mapping import COLUMNS

_FIXTURES = Path(__file__).parent / "fixtures" / "egress"

_OP = COLUMNS.index("GenAiOperation")
_COST = COLUMNS.index("EstimatedCost")
_SYSTEM = COLUMNS.index("GenAiSystem")
_SOURCE = COLUMNS.index("CostSource")
_SPAN_ID = COLUMNS.index("SpanId")
_TENANT = COLUMNS.index("TenantId")


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


# --- fake infra -------------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for ClickHouseStore — records inserted rows, answers span_exists."""

    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def span_exists(self, tenant_id: str, span_id: str) -> bool:
        return any(r[_TENANT] == tenant_id and r[_SPAN_ID] == span_id for r in self.rows)

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int:
        self.rows.extend(rows)
        return len(rows)


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str | None]] = []

    def record_run(self, tenant_id, connector_id, status, *, error_message=None) -> None:
        self.calls.append((tenant_id, connector_id, status, error_message))


class FakeBillingClient:
    """Returns canned DailyCost lists (or raises), never a network call."""

    def __init__(self, costs: list[DailyCost] | None = None, *, raises: Exception | None = None):
        self._costs = costs or []
        self._raises = raises

    def get_daily_costs(self, config, *, start_day, end_day) -> list[DailyCost]:
        if self._raises is not None:
            raise self._raises
        return [c for c in self._costs if start_day <= c.day <= end_day]


def _config(provider: str = "vercel", *, usd_per_gb: Decimal | None = None) -> EgressConfig:
    return EgressConfig(
        tenant_id="t-acme",
        cloud_provider=provider,
        credentials_ref="secret://egress",
        resource_id="zone-123",
        usd_per_gb=usd_per_gb,
    )


# --- fetchers / parsers -----------------------------------------------------------------------


def test_parse_vercel_usage_sums_only_bandwidth() -> None:
    costs = parse_vercel_usage(_fixture("vercel_usage.json"))
    # 3.00 + 1.50 collapse to one 2026-07-01 total; the $99 compute line is IGNORED (no
    # double-count with the compute layer); the $0 day is dropped.
    assert costs == [
        DailyCost(day=date(2026, 7, 1), cost_micro_usd=4_500_000),
        DailyCost(day=date(2026, 7, 2), cost_micro_usd=6_250_000),
    ]


def test_parse_cloudflare_bytes_sums_across_zones() -> None:
    per_day = parse_cloudflare_bytes(_fixture("cloudflare_graphql.json"))
    # Two zones each report 5 GiB on 2026-07-01 → 10 GiB summed; 20 GiB on 07-02; 0 dropped.
    assert per_day == {
        date(2026, 7, 1): 10 * 1024**3,
        date(2026, 7, 2): 20 * 1024**3,
    }


def test_parse_aws_egress_response_daily_totals() -> None:
    # Egress reuses compute's AWS parser — only the Cost Explorer Filter differs.
    costs = parse_aws_cost_response(_fixture("aws_egress_cost_explorer.json"))
    assert costs == [
        DailyCost(day=date(2026, 7, 1), cost_micro_usd=4_000_000),
        DailyCost(day=date(2026, 7, 2), cost_micro_usd=2_750_000),
    ]


def test_cloudflare_client_prices_bytes_at_configured_rate() -> None:
    from gateway.connectors.egress import CloudflareAnalyticsClient

    client = CloudflareAnalyticsClient(
        graphql_runner=lambda cfg, s, e: _fixture("cloudflare_graphql.json")
    )
    costs = client.get_daily_costs(
        _config("cloudflare", usd_per_gb=Decimal("0.10")),
        start_day=date(2026, 7, 1),
        end_day=date(2026, 7, 3),
    )
    # 10 GiB * $0.10 = $1.00 ; 20 GiB * $0.10 = $2.00.
    assert costs == [
        DailyCost(day=date(2026, 7, 1), cost_micro_usd=1_000_000),
        DailyCost(day=date(2026, 7, 2), cost_micro_usd=2_000_000),
    ]


def test_cloudflare_without_rate_fails_soft_no_guess() -> None:
    from gateway.connectors.egress import CloudflareAnalyticsClient

    client = CloudflareAnalyticsClient(
        graphql_runner=lambda cfg, s, e: _fixture("cloudflare_graphql.json")
    )
    # No usd_per_gb configured → refuse to price bytes; the connector turns this into a failed run.
    with pytest.raises(ValueError):
        client.get_daily_costs(
            _config("cloudflare", usd_per_gb=None),
            start_day=date(2026, 7, 1),
            end_day=date(2026, 7, 3),
        )


def test_aws_egress_client_parses_via_injected_session() -> None:
    from gateway.connectors.egress import AwsEgressCostExplorerClient

    class _FakeCe:
        def get_cost_and_usage(self, **kwargs):
            assert kwargs["Granularity"] == "DAILY"
            # Filter must scope to bytes-out egress, not all spend.
            assert "DataTransfer-Out-Bytes" in json.dumps(kwargs["Filter"])
            return _fixture("aws_egress_cost_explorer.json")

    class _FakeSession:
        def client(self, name):
            assert name == "ce"
            return _FakeCe()

    client = AwsEgressCostExplorerClient(session_factory=lambda ref: _FakeSession())
    costs = client.get_daily_costs(_config("aws"), start_day=date(2026, 7, 1), end_day=date(2026, 7, 3))
    assert [c.cost_micro_usd for c in costs] == [4_000_000, 2_750_000]


# --- synthetic span emission ------------------------------------------------------------------


def test_run_emits_one_egress_span_with_direct_cost() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    client = FakeBillingClient([DailyCost(date(2026, 7, 1), 4_500_000)])
    connector = EgressCostConnector(store=store, recorder=recorder, billing_client=client)

    result = connector.run(_config("vercel"), day=date(2026, 7, 1))

    assert result.status == "success"
    assert result.spans_emitted == 1
    row = store.rows[0]
    assert row[_OP] == "egress"
    assert row[_SYSTEM] == "vercel"
    assert row[_SOURCE] == "estimated"
    assert float(row[_COST]) == pytest.approx(4.50)


# --- multi-provider: no double-counting -------------------------------------------------------


def test_multiple_providers_sum_without_double_counting() -> None:
    """Three egress providers on the SAME day → three distinct spans, summing once each."""
    store = FakeStore()
    day = date(2026, 7, 1)
    providers = {
        "vercel": 4_500_000,
        "cloudflare": 1_000_000,
        "aws": 4_000_000,
    }
    for provider, micro in providers.items():
        connector = EgressCostConnector(
            store=store,
            recorder=FakeRecorder(),
            billing_client=FakeBillingClient([DailyCost(day, micro)]),
        )
        cfg = _config(provider, usd_per_gb=Decimal("0.10") if provider == "cloudflare" else None)
        connector.run(cfg, day=day)

    # One span per provider — distinct span ids keyed on provider, no collision, no overwrite.
    assert len(store.rows) == 3
    span_ids = {r[_SPAN_ID] for r in store.rows}
    assert len(span_ids) == 3
    expected_ids = {
        synthetic_span_id("t-acme", p, "egress", day)[1] for p in providers
    }
    assert span_ids == expected_ids
    # The Egress column sums each provider exactly once: 4.50 + 1.00 + 4.00 = 9.50.
    assert sum(float(r[_COST]) for r in store.rows) == pytest.approx(9.50)


def test_rerunning_a_provider_does_not_double_count() -> None:
    store = FakeStore()
    day = date(2026, 7, 1)
    connector = EgressCostConnector(
        store=store,
        recorder=FakeRecorder(),
        billing_client=FakeBillingClient([DailyCost(day, 4_000_000)]),
    )
    first = connector.run(_config("aws"), day=day)
    second = connector.run(_config("aws"), day=day)
    assert first.spans_emitted == 1
    assert second.spans_emitted == 0  # already present → skipped
    assert len(store.rows) == 1


# --- idempotency & backfill -------------------------------------------------------------------


def test_backfill_is_idempotent() -> None:
    store = FakeStore()
    costs = [DailyCost(date(2026, 7, 1), 4_000_000), DailyCost(date(2026, 7, 2), 2_750_000)]
    connector = EgressCostConnector(
        store=store, recorder=FakeRecorder(), billing_client=FakeBillingClient(costs)
    )
    first = connector.run_backfill(_config("aws"), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2))
    second = connector.run_backfill(_config("aws"), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2))
    assert first.spans_emitted == 2
    assert second.spans_emitted == 0
    assert len(store.rows) == 2


def test_egress_span_id_distinct_from_compute_same_day() -> None:
    # The egress layer gets its own id space so it never collides with a compute span.
    egress = synthetic_span_id("t", "aws", "egress", date(2026, 7, 1))
    compute = synthetic_span_id("t", "aws", "compute", date(2026, 7, 1))
    assert egress != compute


# --- run-status recording ---------------------------------------------------------------------


def test_run_records_success_status() -> None:
    recorder = FakeRecorder()
    connector = EgressCostConnector(
        store=FakeStore(),
        recorder=recorder,
        billing_client=FakeBillingClient([DailyCost(date(2026, 7, 1), 4_500_000)]),
    )
    connector.run(_config("vercel"), day=date(2026, 7, 1))
    assert recorder.calls == [("t-acme", "egress", "success", None)]


def test_failed_fetch_records_failed_and_emits_no_span() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    connector = EgressCostConnector(
        store=store,
        recorder=recorder,
        billing_client=FakeBillingClient(raises=RuntimeError("cloudflare 503")),
    )
    result = connector.run(_config("cloudflare", usd_per_gb=Decimal("0.10")), day=date(2026, 7, 1))
    assert result.status == "failed"
    assert result.spans_emitted == 0
    assert store.rows == []
    assert recorder.calls[0][2] == "failed"
    assert "cloudflare 503" in (recorder.calls[0][3] or "")


def test_connector_requires_client() -> None:
    connector = EgressCostConnector(store=FakeStore(), recorder=FakeRecorder(), billing_client=None)
    result = connector.run(_config("vercel"), day=date(2026, 7, 1))
    assert result.status == "failed"
