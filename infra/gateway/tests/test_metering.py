"""Server-side metering: head trace-count, feature-count, rollups, immutability (CTO-84/85/86)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.metering import (
    DEFAULT_PLAN_LIMIT,
    ClosedPeriodError,
    DistinctMeter,
    PlanLimit,
    UsageRollup,
    billing_period,
    commitment,
)

T = "t-acme"


def _ns(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


MAY = _ns(2026, 5, 15)
JUN = _ns(2026, 6, 2)


# --- billing period -------------------------------------------------------------------------------


def test_billing_period_is_utc_month() -> None:
    assert billing_period(MAY) == "2026-05"
    assert billing_period(JUN) == "2026-06"


def test_billing_period_boundary_is_utc() -> None:
    # 2026-06-01 00:00:00 UTC belongs to June, not May.
    assert billing_period(_ns(2026, 6, 1)) == "2026-06"


# --- distinct meter -------------------------------------------------------------------------------


def test_distinct_meter_counts_distinct_and_is_idempotent() -> None:
    m = DistinctMeter()
    assert m.record(T, "trace_1", period="2026-05") is True
    assert m.record(T, "trace_2", period="2026-05") is True
    # redelivery of the same id must not inflate the count (at-least-once safe).
    assert m.record(T, "trace_1", period="2026-05") is False
    assert m.count(T, "2026-05") == 2


def test_distinct_meter_is_tenant_and_period_scoped() -> None:
    m = DistinctMeter()
    m.record(T, "trace_1", period="2026-05")
    m.record(T, "trace_1", period="2026-06")  # different period: counted again
    m.record("t-other", "trace_1", period="2026-05")  # different tenant: isolated
    assert m.count(T, "2026-05") == 1
    assert m.count(T, "2026-06") == 1
    assert m.count("t-other", "2026-05") == 1


# --- tamper-evidence ------------------------------------------------------------------------------


def test_commitment_is_order_independent() -> None:
    assert commitment({"a", "b", "c"}) == commitment({"c", "a", "b"})


def test_commitment_changes_when_a_record_is_dropped() -> None:
    full = commitment({"a", "b", "c"})
    tampered = commitment({"a", "b"})  # someone dropped a billable trace
    assert full != tampered


def test_commitment_detects_injected_record() -> None:
    assert commitment({"a", "b"}) != commitment({"a", "b", "x"})


def test_meter_commitment_reconciles_against_raw_ingest() -> None:
    m = DistinctMeter()
    raw = {"trace_1", "trace_2", "trace_3"}
    for tid in raw:
        m.record(T, tid, period="2026-05")
    # Recomputing the commitment over the raw distinct set must match the meter's; that is the
    # reconciliation check billing runs to prove the count wasn't tampered with.
    assert m.commitment(T, "2026-05") == commitment(raw)


# --- rollups + sampling independence (CTO-84) -----------------------------------------------------


def test_head_count_is_independent_of_sampling() -> None:
    roll = UsageRollup()
    # Meter every trace at HEAD, then a sampler keeps only 1-in-10 for analytics. The billed count
    # must stay at the full number; sampling down does not reduce the bill.
    for i in range(100):
        trace_id = f"trace_{i}"
        roll.record_trace(T, trace_id, MAY)
        _analytics_keep = (i % 10 == 0)  # noqa: F841 - models a sampling decision made downstream
    assert roll.usage(T, "2026-05").trace_count == 100


def test_record_span_counts_trace_and_feature() -> None:
    roll = UsageRollup()
    roll.record_span(T, trace_id="trace_1", feature_tag="checkout", ts_ns=MAY)
    roll.record_span(T, trace_id="trace_2", feature_tag="checkout", ts_ns=MAY)
    roll.record_span(T, trace_id="trace_3", feature_tag="search", ts_ns=MAY)
    usage = roll.usage(T, "2026-05")
    assert usage.trace_count == 3
    assert usage.feature_count == 2  # distinct: {checkout, search}


def test_record_span_ignores_empty_ids() -> None:
    roll = UsageRollup()
    roll.record_span(T, trace_id=None, feature_tag=None, ts_ns=MAY)
    roll.record_span(T, trace_id="", feature_tag="", ts_ns=MAY)
    usage = roll.usage(T, "2026-05")
    assert usage.trace_count == 0
    assert usage.feature_count == 0


# --- usage API + plan limits (CTO-86) -------------------------------------------------------------


def test_usage_reports_plan_limit_and_overage() -> None:
    roll = UsageRollup()
    roll.set_plan(T, PlanLimit(plan="starter", trace_limit=2, feature_limit=1))
    roll.record_span(T, trace_id="a", feature_tag="f1", ts_ns=MAY)
    roll.record_span(T, trace_id="b", feature_tag="f2", ts_ns=MAY)
    roll.record_span(T, trace_id="c", feature_tag="f3", ts_ns=MAY)
    usage = roll.usage(T, "2026-05")
    assert usage.plan == "starter"
    assert usage.trace_limit == 2
    assert usage.over_trace_limit is True  # 3 > 2
    assert usage.over_feature_limit is True  # 3 > 1


def test_usage_defaults_to_plan_when_unset() -> None:
    roll = UsageRollup()
    usage = roll.usage(T, "2026-05")
    assert usage.plan == DEFAULT_PLAN_LIMIT.plan
    assert usage.trace_limit == DEFAULT_PLAN_LIMIT.trace_limit


def test_usage_defaults_to_current_period() -> None:
    roll = UsageRollup(now_ns=lambda: MAY)
    roll.record_trace(T, "trace_1", MAY)
    usage = roll.usage(T)  # no period → current
    assert usage.period == "2026-05"
    assert usage.trace_count == 1


def test_usage_as_dict_is_json_friendly() -> None:
    roll = UsageRollup()
    roll.record_span(T, trace_id="a", feature_tag="f1", ts_ns=MAY)
    d = roll.usage(T, "2026-05").as_dict()
    assert d["tenant_id"] == T
    assert d["period"] == "2026-05"
    assert d["trace_count"] == 1
    assert set(d) >= {"trace_commitment", "feature_commitment", "closed", "over_trace_limit"}


# --- closed-period immutability (CTO-86) ----------------------------------------------------------


def test_close_period_freezes_an_immutable_snapshot() -> None:
    roll = UsageRollup()
    roll.record_trace(T, "trace_1", MAY)
    closed = roll.close_period(T, "2026-05")
    assert closed.closed is True
    assert closed.trace_count == 1


def test_recording_into_closed_period_raises() -> None:
    roll = UsageRollup()
    roll.record_trace(T, "trace_1", MAY)
    roll.close_period(T, "2026-05")
    with pytest.raises(ClosedPeriodError):
        roll.record_trace(T, "trace_2", MAY)


def test_close_period_is_idempotent() -> None:
    roll = UsageRollup()
    roll.record_trace(T, "trace_1", MAY)
    first = roll.close_period(T, "2026-05")
    second = roll.close_period(T, "2026-05")
    assert first == second


def test_other_period_still_open_after_close() -> None:
    roll = UsageRollup()
    roll.record_trace(T, "trace_1", MAY)
    roll.close_period(T, "2026-05")
    # June is unaffected by closing May.
    roll.record_trace(T, "trace_2", JUN)
    assert roll.usage(T, "2026-06").trace_count == 1


# --- synthetic trace ids are not billable traces (CTO-401) ----------------------------------------


def test_synthetic_trace_id_is_not_counted() -> None:
    roll = UsageRollup()
    for i in range(300):
        roll.record_span(
            T, trace_id=f"syn_{i}", feature_tag=None, ts_ns=MAY, trace_id_synthetic=True
        )
    # Exactly the pre-CTO-396 head-meter behaviour for trace-less traffic: contributes nothing.
    assert roll.usage(T, "2026-05").trace_count == 0


def test_synthetic_trace_id_still_meters_its_feature_tag() -> None:
    roll = UsageRollup()
    roll.record_span(
        T, trace_id="syn_1", feature_tag="checkout", ts_ns=MAY, trace_id_synthetic=True
    )
    usage = roll.usage(T, "2026-05")
    assert usage.trace_count == 0
    assert usage.feature_count == 1


def test_real_trace_is_still_counted_alongside_synthetic_ones() -> None:
    roll = UsageRollup()
    roll.record_span(T, trace_id="real_1", feature_tag=None, ts_ns=MAY)
    for i in range(50):
        roll.record_span(
            T, trace_id=f"syn_{i}", feature_tag=None, ts_ns=MAY, trace_id_synthetic=True
        )
    assert roll.usage(T, "2026-05").trace_count == 1


# --- bounded memory (CTO-401) ---------------------------------------------------------------------


def test_distinct_meter_memory_is_bounded_for_a_million_ids() -> None:
    """The set must stop growing at the cap, asserted on the set itself rather than by timing."""
    cap = 1_000
    m = DistinctMeter(max_ids_per_period=cap)
    for i in range(1_000_000):
        m.record(T, f"syn_{i}", period="2026-05")
    assert m.count(T, "2026-05") == cap
    # The bound that matters is the live id set: 1M distinct ids interned exactly `cap` strings.
    assert len(m._ids[(T, "2026-05")]) == cap


def test_saturated_meter_declares_itself_inexact() -> None:
    m = DistinctMeter(max_ids_per_period=2)
    m.record(T, "a", period="2026-05")
    m.record(T, "b", period="2026-05")
    assert m.exact(T, "2026-05") is True
    m.record(T, "c", period="2026-05")  # past the cap
    assert m.exact(T, "2026-05") is False
    # A commitment over a set that is missing members would reconcile against nothing while looking
    # exactly like one that does, so it is absent rather than wrong.
    assert m.commitment(T, "2026-05") is None


def test_unsaturated_meter_keeps_its_commitment() -> None:
    m = DistinctMeter(max_ids_per_period=10)
    m.record(T, "a", period="2026-05")
    assert m.commitment(T, "2026-05") == commitment({"a"})
    assert m.exact(T, "2026-05") is True


def test_usage_record_reports_count_exactness() -> None:
    roll = UsageRollup(max_ids_per_period=1)
    roll.record_trace(T, "a", MAY)
    assert roll.usage(T, "2026-05").trace_count_exact is True
    roll.record_trace(T, "b", MAY)  # saturates
    usage = roll.usage(T, "2026-05")
    assert usage.trace_count_exact is False
    assert usage.trace_commitment is None
    assert usage.as_dict()["trace_count_exact"] is False


def test_saturation_cannot_hide_an_overage() -> None:
    """The cap sits far above every plan ceiling, so the over-limit decision is unchanged."""
    roll = UsageRollup(max_ids_per_period=10)
    roll.set_plan(T, PlanLimit(plan="starter", trace_limit=2, feature_limit=1))
    for i in range(50):
        roll.record_trace(T, f"trace_{i}", MAY)
    usage = roll.usage(T, "2026-05")
    assert usage.trace_count == 10  # a floor, and it says so
    assert usage.trace_count_exact is False
    assert usage.over_trace_limit is True  # still unambiguously over 2
