"""Reconciler: tag→feature mapping, shared-cost allocation, billing-lag gating, deltas (CTO-64)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tally.reconciler import (
    UNATTRIBUTED,
    CloudBillingLineItem,
    CostSource,
    EstimatedCostRow,
    ReconciledCostRow,
    ReconcilerConfig,
    ReconciliationDelta,
    ReconciliationReport,
    reconcile,
)

DAY = date(2026, 5, 1)
# 48h+ after DAY closed (midnight 2026-05-02) → settled under the default lag
SETTLED_AS_OF = datetime(2026, 5, 5, tzinfo=timezone.utc)


def _rows(report: ReconciliationReport) -> dict[tuple[str, date], ReconciledCostRow]:
    return {(r.feature_tag, r.day): r for r in report.rows}


# --- direct mapping -----------------------------------------------------------------------------


def test_directly_tagged_billing_trues_up_the_feature() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000, query_count=10)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_250_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    row = _rows(report)[("chat", DAY)]
    assert row.cost_source is CostSource.RECONCILED
    assert row.settled
    assert row.estimated_micro_usd == 1_000_000
    assert row.reconciled_micro_usd == 1_250_000
    assert row.delta_micro_usd == 250_000
    assert row.delta_pct == pytest.approx(25.0)


def test_emits_delta_event_with_summary() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_500_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert isinstance(delta, ReconciliationDelta)
    s = delta.summary()
    assert "est $1" in s
    assert "reconciled $1.50" in s
    assert "+50.0%" in s


def test_no_delta_when_reconciled_equals_estimated() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=900_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=900_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    assert report.deltas == ()
    assert _rows(report)[("chat", DAY)].delta_micro_usd == 0


# --- shared-cost allocation by query count ------------------------------------------------------


def test_shared_pool_allocated_by_query_count() -> None:
    # untagged "shared-db" bill of 3.00 split across two features by query weight 3:1
    est = [
        EstimatedCostRow("chat", DAY, estimated_micro_usd=0, query_count=30),
        EstimatedCostRow("search", DAY, estimated_micro_usd=0, query_count=10),
    ]
    billing = [CloudBillingLineItem("shared-db", DAY, cost_micro_usd=3_000_000)]
    report = reconcile(est, billing, tag_map={}, as_of=SETTLED_AS_OF)
    rows = _rows(report)
    assert rows[("chat", DAY)].reconciled_micro_usd == 2_250_000  # 3/4
    assert rows[("search", DAY)].reconciled_micro_usd == 750_000  # 1/4
    # allocation conserves the pool exactly
    assert (
        rows[("chat", DAY)].reconciled_micro_usd
        + rows[("search", DAY)].reconciled_micro_usd
        == 3_000_000
    )


def test_allocation_remainder_is_conserved() -> None:
    # 1 micro-USD split 3 ways by equal weight → parts must still sum to 1
    est = [
        EstimatedCostRow("a", DAY, estimated_micro_usd=0, query_count=1),
        EstimatedCostRow("b", DAY, estimated_micro_usd=0, query_count=1),
        EstimatedCostRow("c", DAY, estimated_micro_usd=0, query_count=1),
    ]
    billing = [CloudBillingLineItem("shared", DAY, cost_micro_usd=1)]
    report = reconcile(est, billing, tag_map={}, as_of=SETTLED_AS_OF)
    total = sum(r.reconciled_micro_usd for r in report.rows)
    assert total == 1


def test_shared_pool_with_no_query_signal_splits_evenly() -> None:
    est = [
        EstimatedCostRow("a", DAY, estimated_micro_usd=0, query_count=0),
        EstimatedCostRow("b", DAY, estimated_micro_usd=0, query_count=0),
    ]
    billing = [CloudBillingLineItem("shared", DAY, cost_micro_usd=1_000_000)]
    report = reconcile(est, billing, tag_map={}, as_of=SETTLED_AS_OF)
    rows = _rows(report)
    assert rows[("a", DAY)].reconciled_micro_usd == 500_000
    assert rows[("b", DAY)].reconciled_micro_usd == 500_000


def test_shared_pool_with_no_features_goes_unattributed() -> None:
    billing = [CloudBillingLineItem("mystery", DAY, cost_micro_usd=400_000)]
    report = reconcile([], billing, tag_map={}, as_of=SETTLED_AS_OF)
    rows = _rows(report)
    assert rows[(UNATTRIBUTED, DAY)].reconciled_micro_usd == 400_000


def test_direct_plus_shared_combine() -> None:
    est = [
        EstimatedCostRow("chat", DAY, estimated_micro_usd=500_000, query_count=10),
        EstimatedCostRow("search", DAY, estimated_micro_usd=200_000, query_count=10),
    ]
    billing = [
        CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=600_000),  # direct → chat
        CloudBillingLineItem("shared-db", DAY, cost_micro_usd=200_000),  # split 50/50
    ]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    rows = _rows(report)
    assert rows[("chat", DAY)].reconciled_micro_usd == 700_000  # 600k direct + 100k shared
    assert rows[("search", DAY)].reconciled_micro_usd == 100_000  # 0 direct + 100k shared


# --- billing lag gating -------------------------------------------------------------------------


def test_unsettled_day_stays_estimated_and_is_skipped() -> None:
    # job runs only 1h after the day closed — invoice not final yet
    as_of = datetime(2026, 5, 2, 1, tzinfo=timezone.utc)
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=9_999_999)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=as_of)
    row = _rows(report)[("chat", DAY)]
    assert row.cost_source is CostSource.ESTIMATED
    assert not row.settled
    assert row.reconciled_micro_usd == 1_000_000  # untouched by the not-yet-final bill
    assert DAY in report.skipped_unsettled_days
    assert report.deltas == ()


def test_custom_lag_hours_gates_settlement() -> None:
    # with a 0h lag, a day is settled the moment it closes
    as_of = datetime(2026, 5, 2, 0, tzinfo=timezone.utc)
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_200_000)]
    report = reconcile(
        est, billing, tag_map={"svc-chat": "chat"}, as_of=as_of,
        config=ReconcilerConfig(lag_hours=0),
    )
    assert _rows(report)[("chat", DAY)].cost_source is CostSource.RECONCILED


def test_mixed_settled_and_unsettled_days() -> None:
    old_day = date(2026, 5, 1)
    new_day = date(2026, 5, 4)
    as_of = datetime(2026, 5, 5, tzinfo=timezone.utc)  # old settled, new not (only ~1 day)
    est = [
        EstimatedCostRow("chat", old_day, estimated_micro_usd=1_000_000),
        EstimatedCostRow("chat", new_day, estimated_micro_usd=1_000_000),
    ]
    billing = [
        CloudBillingLineItem("svc-chat", old_day, cost_micro_usd=1_100_000),
        CloudBillingLineItem("svc-chat", new_day, cost_micro_usd=1_100_000),
    ]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=as_of)
    rows = _rows(report)
    assert rows[("chat", old_day)].settled
    assert not rows[("chat", new_day)].settled
    assert new_day in report.skipped_unsettled_days
    assert old_day not in report.skipped_unsettled_days


# --- aggregation / folding ----------------------------------------------------------------------


def test_repeated_feature_day_rows_are_folded() -> None:
    est = [
        EstimatedCostRow("chat", DAY, estimated_micro_usd=400_000, query_count=4),
        EstimatedCostRow("chat", DAY, estimated_micro_usd=600_000, query_count=6),
    ]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_000_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    row = _rows(report)[("chat", DAY)]
    assert row.estimated_micro_usd == 1_000_000  # folded


def test_report_totals_and_summary() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_200_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    assert report.total_estimated_micro_usd == 1_000_000
    assert report.total_reconciled_micro_usd == 1_200_000
    s = report.summary()
    assert "reconciled" in s
    d = report.as_dict()
    assert isinstance(d["rows"], list)
    assert isinstance(d["deltas"], list)
    assert "summary" in d


# --- delta percentage edge cases ----------------------------------------------------------------


def test_delta_pct_none_when_estimate_is_zero() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=0, query_count=1)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=500_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)
    delta = report.deltas[0]
    assert delta.delta_pct is None
    assert "new" in delta.summary()


# --- never-crash / validation -------------------------------------------------------------------


def test_empty_inputs_return_empty_report() -> None:
    report = reconcile([], [], as_of=SETTLED_AS_OF)
    assert isinstance(report, ReconciliationReport)
    assert report.rows == ()
    assert report.deltas == ()
    assert report.total_reconciled_micro_usd == 0


def test_garbage_entries_ignored() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000), "junk", None]
    billing = ["nope", CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_000)]
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=SETTLED_AS_OF)  # type: ignore[list-item]
    assert len(report.rows) == 1


def test_naive_as_of_is_treated_as_utc() -> None:
    est = [EstimatedCostRow("chat", DAY, estimated_micro_usd=1_000_000)]
    billing = [CloudBillingLineItem("svc-chat", DAY, cost_micro_usd=1_000_000)]
    naive = datetime(2026, 5, 5)  # no tzinfo
    report = reconcile(est, billing, tag_map={"svc-chat": "chat"}, as_of=naive)
    assert _rows(report)[("chat", DAY)].settled


def test_invalid_estimated_cost_raises() -> None:
    with pytest.raises(ValueError):
        EstimatedCostRow("chat", DAY, estimated_micro_usd=-1)


def test_invalid_query_count_raises() -> None:
    with pytest.raises(ValueError):
        EstimatedCostRow("chat", DAY, estimated_micro_usd=0, query_count=-5)


def test_bool_cost_rejected() -> None:
    with pytest.raises(ValueError):
        CloudBillingLineItem("svc", DAY, cost_micro_usd=True)  # type: ignore[arg-type]


def test_empty_feature_tag_raises() -> None:
    with pytest.raises(ValueError):
        EstimatedCostRow("", DAY, estimated_micro_usd=0)


def test_invalid_lag_raises() -> None:
    with pytest.raises(ValueError):
        ReconcilerConfig(lag_hours=-1)


def test_types_are_frozen() -> None:
    assert ReconciledCostRow.__hash__ is not None
    assert ReconciliationDelta.__hash__ is not None
    assert EstimatedCostRow.__hash__ is not None
