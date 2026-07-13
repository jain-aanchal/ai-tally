"""Compute cost-layer connector (CTO-143) — offline tests, NO live cloud calls.

Pins the contract the /cost Compute column depends on:

  * AWS Cost Explorer + GCP Cloud Billing fixtures parse to the correct per-day micro-USD totals.
  * The connector lands ONE synthetic span per day with GenAiOperation='compute' and the day's total
    as EstimatedCost (cost set directly — no catalog enrichment).
  * Backfill is idempotent: running twice does not double-count (the deterministic span-id guard).
  * Run status is recorded on success and failure; a failed fetch emits NO span (never a guess).

Everything provider-specific is behind an injected fake ``BillingClient`` / fake store, so no test
touches boto3, BigQuery, ClickHouse, or Postgres.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gateway.connectors.base import ConnectorConfig, DailyCost, synthetic_span_id
from gateway.connectors.compute import (
    ComputeCostConnector,
    parse_aws_cost_response,
    parse_gcp_billing_rows,
)
from gateway.mapping import COLUMNS

_FIXTURES = Path(__file__).parent / "fixtures" / "compute"

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


def _config(provider: str = "aws") -> ConnectorConfig:
    return ConnectorConfig(
        tenant_id="t-acme",
        cloud_provider=provider,
        credentials_ref="aws-default-chain",
        tag_filter={"tally:workload": "ai"},
    )


# --- fetchers / parsers -----------------------------------------------------------------------


def test_parse_aws_cost_response_daily_totals() -> None:
    costs = parse_aws_cost_response(_fixture("aws_cost_explorer.json"))
    # The $0 day is dropped — a zero-cost day carries no synthetic span.
    assert costs == [
        DailyCost(day=date(2026, 7, 1), cost_micro_usd=12_500_000),
        DailyCost(day=date(2026, 7, 2), cost_micro_usd=8_250_000),
    ]


def test_parse_gcp_billing_rows_sums_same_day() -> None:
    costs = parse_gcp_billing_rows(_fixture("gcp_billing_rows.json"))
    # 3.00 + 4.50 collapse to one 2026-07-01 total; the $0 row is dropped.
    assert costs == [
        DailyCost(day=date(2026, 7, 1), cost_micro_usd=7_500_000),
        DailyCost(day=date(2026, 7, 2), cost_micro_usd=8_250_000),
    ]


def test_aws_client_wrapper_parses_via_injected_session() -> None:
    # Prove the SDK-touching client never needs boto3: inject a fake ``ce`` client whose
    # get_cost_and_usage returns the recorded fixture.
    from gateway.connectors.compute import AwsCostExplorerClient

    class _FakeCe:
        def get_cost_and_usage(self, **kwargs):
            assert kwargs["Granularity"] == "DAILY"
            return _fixture("aws_cost_explorer.json")

    class _FakeSession:
        def client(self, name):
            assert name == "ce"
            return _FakeCe()

    client = AwsCostExplorerClient(session_factory=lambda ref: _FakeSession())
    costs = client.get_daily_costs(_config(), start_day=date(2026, 7, 1), end_day=date(2026, 7, 3))
    assert [c.cost_micro_usd for c in costs] == [12_500_000, 8_250_000]


# --- synthetic span emission ------------------------------------------------------------------


def test_run_emits_one_compute_span_with_direct_cost() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    client = FakeBillingClient([DailyCost(date(2026, 7, 1), 12_500_000)])
    connector = ComputeCostConnector(store=store, recorder=recorder, billing_client=client)

    result = connector.run(_config("aws"), day=date(2026, 7, 1))

    assert result.status == "success"
    assert result.spans_emitted == 1
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row[_OP] == "compute"
    assert row[_SYSTEM] == "aws"
    assert row[_SOURCE] == "estimated"
    # EstimatedCost is the Decimal64(8) USD value — 12_500_000 micro-USD == $12.50.
    assert float(row[_COST]) == pytest.approx(12.50)


def test_zero_cost_day_emits_no_span() -> None:
    store = FakeStore()
    connector = ComputeCostConnector(
        store=store, recorder=FakeRecorder(), billing_client=FakeBillingClient([])
    )
    connector.emit_cost_span("t-acme", cost_micro_usd=0, day=date(2026, 7, 1), provider="aws")
    assert store.rows == []


# --- idempotency ------------------------------------------------------------------------------


def test_backfill_is_idempotent() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    costs = [
        DailyCost(date(2026, 7, 1), 12_500_000),
        DailyCost(date(2026, 7, 2), 8_250_000),
    ]
    connector = ComputeCostConnector(
        store=store, recorder=recorder, billing_client=FakeBillingClient(costs)
    )

    first = connector.run_backfill(_config(), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2))
    second = connector.run_backfill(_config(), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2))

    assert first.spans_emitted == 2
    assert second.spans_emitted == 0  # already present → skipped, not re-inserted
    assert len(store.rows) == 2  # still just the two days, no double-count


def test_synthetic_span_id_is_deterministic_and_operation_scoped() -> None:
    a = synthetic_span_id("t", "aws", "compute", date(2026, 7, 1))
    b = synthetic_span_id("t", "aws", "compute", date(2026, 7, 1))
    other_op = synthetic_span_id("t", "aws", "egress", date(2026, 7, 1))
    assert a == b
    assert a != other_op  # egress (CTO-144) gets its own id space for the same day


# --- run-status recording ---------------------------------------------------------------------


def test_run_records_success_status() -> None:
    recorder = FakeRecorder()
    connector = ComputeCostConnector(
        store=FakeStore(),
        recorder=recorder,
        billing_client=FakeBillingClient([DailyCost(date(2026, 7, 1), 12_500_000)]),
    )
    connector.run(_config(), day=date(2026, 7, 1))
    assert recorder.calls == [("t-acme", "compute", "success", None)]


def test_failed_fetch_records_failed_and_emits_no_span() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    connector = ComputeCostConnector(
        store=store,
        recorder=recorder,
        billing_client=FakeBillingClient(raises=RuntimeError("cost explorer 503")),
    )

    result = connector.run(_config(), day=date(2026, 7, 1))

    assert result.status == "failed"
    assert result.spans_emitted == 0
    assert store.rows == []  # honest-under-uncertainty: never a guessed number
    assert recorder.calls[0][2] == "failed"
    assert "cost explorer 503" in (recorder.calls[0][3] or "")


def test_connector_requires_operation_and_client() -> None:
    # The base refuses to run a compute fetch with no client wired.
    connector = ComputeCostConnector(store=FakeStore(), recorder=FakeRecorder(), billing_client=None)
    result = connector.run(_config(), day=date(2026, 7, 1))
    assert result.status == "failed"
