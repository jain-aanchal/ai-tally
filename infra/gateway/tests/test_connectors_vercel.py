"""Vercel compute + egress cost connector (CTO-163) — offline tests, NO live cloud calls.

Pins the contract the /cost Compute + Egress columns depend on for a Vercel-hosted app:

  * The SAME Vercel usage payload splits into compute (Function invocations + GB-hours) and egress
    (bandwidth) with NO cross-layer double-count — compute ignores bandwidth, egress ignores compute.
  * The connector lands ONE synthetic ``compute`` span/day (GenAiSystem='vercel') with the day's
    compute total as EstimatedCost (cost set directly — no catalog enrichment).
  * Egress double-count reconciliation with CTO-144: by default (``emit_egress=False``) the connector
    emits NO egress span — CTO-144's egress connector owns it. With the gate on it DOES emit egress,
    and the span id is IDENTICAL to CTO-144's, so the base span_exists guard collapses any overlap.
  * Backfill is idempotent; a failed fetch records 'failed' and emits NO span.

Reuses the CTO-143 base + CTO-144 egress connector verbatim: everything Vercel-specific is behind an
injected fake usage fetcher / fake store — no test touches requests, ClickHouse, or Postgres.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gateway.connectors.base import synthetic_span_id
from gateway.connectors.egress import (
    EgressCostConnector,
    VercelBandwidthClient,
    parse_vercel_usage,
)
from gateway.connectors.vercel import (
    VercelComputeClient,
    VercelConfig,
    VercelCostConnector,
    VercelUsageClient,
    parse_vercel_compute,
)
from gateway.mapping import COLUMNS

_FIXTURES = Path(__file__).parent / "fixtures" / "vercel"

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


def _usage_client(payload: object | None = None, *, raises: Exception | None = None):
    """A VercelUsageClient whose fetch returns the fixture (or raises), never hitting the network."""

    def _getter(config, start_day, end_day):
        if raises is not None:
            raise raises
        return payload if payload is not None else _fixture("usage.json")

    return VercelUsageClient(http_getter=_getter)


def _config(*, emit_egress: bool = False) -> VercelConfig:
    return VercelConfig(
        tenant_id="t-acme",
        cloud_provider="vercel",
        credentials_ref="secret://vercel-token",
        team_id="team_123",
        project_id="prj_456",
        emit_egress=emit_egress,
    )


# --- pure parsers: the compute/egress split, no double-count -----------------------------------


def test_parse_vercel_compute_sums_only_function_compute() -> None:
    costs = parse_vercel_compute(_fixture("usage.json"))
    # 2026-07-01: invocations 2.00 + GB-hours 3.50 = 5.50; 07-02: 4.00; the $99-equivalent bandwidth
    # lines are IGNORED (no double-count with egress); the $0 day is dropped.
    assert costs == [
        _dc(date(2026, 7, 1), 5_500_000),
        _dc(date(2026, 7, 2), 4_000_000),
    ]


def test_compute_and_egress_split_the_same_payload_disjointly() -> None:
    """Compute keeps function_* lines; egress keeps bandwidth lines — no item counts twice."""
    payload = _fixture("usage.json")
    compute = parse_vercel_compute(payload)
    egress = parse_vercel_usage(payload)  # CTO-144's reciprocal parser
    assert [(c.day, c.cost_micro_usd) for c in compute] == [
        (date(2026, 7, 1), 5_500_000),
        (date(2026, 7, 2), 4_000_000),
    ]
    assert [(c.day, c.cost_micro_usd) for c in egress] == [
        (date(2026, 7, 1), 1_250_000),
        (date(2026, 7, 2), 6_250_000),
    ]


def test_compute_client_range_filters() -> None:
    client = VercelComputeClient(_usage_client())
    costs = client.get_daily_costs(_config(), start_day=date(2026, 7, 2), end_day=date(2026, 7, 2))
    assert costs == [_dc(date(2026, 7, 2), 4_000_000)]


# --- synthetic span emission (compute) --------------------------------------------------------


def test_run_emits_one_compute_span_with_direct_cost() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    connector = VercelCostConnector(store=store, usage_client=_usage_client(), recorder=recorder)

    result = connector.run(_config(), day=date(2026, 7, 1))

    assert result.compute.status == "success"
    assert result.compute.spans_emitted == 1
    assert result.egress is None  # gate off by default
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row[_OP] == "compute"
    assert row[_SYSTEM] == "vercel"
    assert row[_SOURCE] == "estimated"
    assert float(row[_COST]) == pytest.approx(5.50)


def test_default_gate_emits_no_egress_span() -> None:
    """emit_egress=False → CTO-144 owns Vercel egress; this connector emits ONLY the compute span."""
    store = FakeStore()
    connector = VercelCostConnector(store=store, usage_client=_usage_client())
    connector.run(_config(emit_egress=False), day=date(2026, 7, 1))
    ops = {r[_OP] for r in store.rows}
    assert ops == {"compute"}


# --- egress reconciliation with CTO-144 -------------------------------------------------------


def test_gated_egress_emits_compute_and_egress_one_each() -> None:
    store = FakeStore()
    connector = VercelCostConnector(store=store, usage_client=_usage_client())
    result = connector.run(_config(emit_egress=True), day=date(2026, 7, 1))

    assert result.compute.spans_emitted == 1
    assert result.egress is not None and result.egress.spans_emitted == 1
    by_op = {r[_OP]: r for r in store.rows}
    assert set(by_op) == {"compute", "egress"}
    assert float(by_op["compute"][_COST]) == pytest.approx(5.50)
    assert float(by_op["egress"][_COST]) == pytest.approx(1.25)


def test_gated_egress_span_id_matches_cto144_path() -> None:
    """The gated egress span is byte-identical in id to CTO-144's egress connector for the same day,
    so if BOTH ever ran the base span_exists guard collapses them — no double-count is possible."""
    day = date(2026, 7, 1)
    store = FakeStore()

    # This connector, gate on.
    VercelCostConnector(store=store, usage_client=_usage_client()).run(
        _config(emit_egress=True), day=day
    )
    egress_row = next(r for r in store.rows if r[_OP] == "egress")
    ours = egress_row[_SPAN_ID]

    # Independently, CTO-144's egress connector with the same Vercel payload.
    store2 = FakeStore()
    EgressCostConnector(
        store=store2,
        recorder=FakeRecorder(),
        billing_client=VercelBandwidthClient(http_getter=_usage_client()._http_getter),
    ).run(VercelCostConnector._egress_config(_config()), day=day)
    theirs = next(r for r in store2.rows if r[_OP] == "egress")[_SPAN_ID]

    assert ours == theirs == synthetic_span_id("t-acme", "vercel", "egress", day)[1]


def test_both_paths_same_store_do_not_double_count_egress() -> None:
    """Belt-and-braces: run CTO-144's egress AND this connector's gated egress on one store; the
    second insert is skipped by span_exists — one egress row total."""
    day = date(2026, 7, 1)
    store = FakeStore()
    EgressCostConnector(
        store=store,
        recorder=FakeRecorder(),
        billing_client=VercelBandwidthClient(http_getter=_usage_client()._http_getter),
    ).run(VercelCostConnector._egress_config(_config()), day=day)
    VercelCostConnector(store=store, usage_client=_usage_client()).run(
        _config(emit_egress=True), day=day
    )
    egress_rows = [r for r in store.rows if r[_OP] == "egress"]
    assert len(egress_rows) == 1


def test_compute_and_egress_span_ids_distinct_same_day() -> None:
    compute = synthetic_span_id("t", "vercel", "compute", date(2026, 7, 1))
    egress = synthetic_span_id("t", "vercel", "egress", date(2026, 7, 1))
    assert compute != egress


# --- idempotency & backfill -------------------------------------------------------------------


def test_backfill_is_idempotent() -> None:
    store = FakeStore()
    connector = VercelCostConnector(store=store, usage_client=_usage_client())
    first = connector.run_backfill(
        _config(), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2)
    )
    second = connector.run_backfill(
        _config(), start_day=date(2026, 7, 1), end_day=date(2026, 7, 2)
    )
    assert first.compute.spans_emitted == 2
    assert second.compute.spans_emitted == 0
    assert len([r for r in store.rows if r[_OP] == "compute"]) == 2


# --- run-status recording ---------------------------------------------------------------------


def test_run_records_success_status() -> None:
    recorder = FakeRecorder()
    connector = VercelCostConnector(
        store=FakeStore(), usage_client=_usage_client(), recorder=recorder
    )
    connector.run(_config(), day=date(2026, 7, 1))
    assert recorder.calls == [("t-acme", "compute", "success", None)]


def test_failed_fetch_records_failed_and_emits_no_span() -> None:
    store, recorder = FakeStore(), FakeRecorder()
    connector = VercelCostConnector(
        store=store,
        usage_client=_usage_client(raises=RuntimeError("vercel 503")),
        recorder=recorder,
    )
    result = connector.run(_config(), day=date(2026, 7, 1))
    assert result.compute.status == "failed"
    assert result.compute.spans_emitted == 0
    assert store.rows == []
    assert recorder.calls[0][2] == "failed"
    assert "vercel 503" in (recorder.calls[0][3] or "")


# --- helpers ----------------------------------------------------------------------------------


def _dc(day, micro):
    from gateway.connectors.base import DailyCost

    return DailyCost(day=day, cost_micro_usd=micro)
