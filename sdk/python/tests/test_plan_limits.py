# SPDX-License-Identifier: Apache-2.0
"""Tests for plan tiers + graceful usage-limit enforcement (CTO-89)."""

from __future__ import annotations

import dataclasses

import pytest

from tally.plan_limits import (
    BUILTIN_TIERS,
    ENTERPRISE,
    FREE,
    PRO,
    SCALE,
    EnforcementDecision,
    EnforcementLevel,
    InMemoryPlanRegistry,
    LimitState,
    Meter,
    PlanRegistry,
    PlanTier,
    UsageSnapshot,
    apply_tier_change,
    build_limit_state,
    classify,
    next_tier_up,
)


# --------------------------------------------------------------------------- #
# Tier definitions + validation
# --------------------------------------------------------------------------- #
def test_builtin_tiers_present_and_ordered() -> None:
    assert BUILTIN_TIERS == (FREE, PRO, SCALE, ENTERPRISE)
    ranks = [t.rank for t in BUILTIN_TIERS]
    assert ranks == sorted(ranks)
    assert FREE.max_traces_per_period < PRO.max_traces_per_period < SCALE.max_traces_per_period


def test_enterprise_is_unlimited_on_both_meters() -> None:
    assert ENTERPRISE.is_unlimited(Meter.TRACES)
    assert ENTERPRISE.is_unlimited(Meter.FEATURES)
    assert ENTERPRISE.limit_for(Meter.TRACES) is None
    assert ENTERPRISE.limit_for(Meter.FEATURES) is None


def test_custom_tier_definition() -> None:
    tier = PlanTier(name="CUSTOM", max_traces_per_period=500, max_features=7, rank=5)
    assert tier.limit_for(Meter.TRACES) == 500
    assert tier.limit_for(Meter.FEATURES) == 7
    assert not tier.is_unlimited(Meter.TRACES)


def test_empty_tier_name_raises() -> None:
    with pytest.raises(ValueError):
        PlanTier(name="", max_traces_per_period=10, max_features=1)
    with pytest.raises(ValueError):
        PlanTier(name="   ", max_traces_per_period=10, max_features=1)


def test_negative_limits_raise() -> None:
    with pytest.raises(ValueError):
        PlanTier(name="X", max_traces_per_period=-1, max_features=1)
    with pytest.raises(ValueError):
        PlanTier(name="X", max_traces_per_period=1, max_features=-1)
    with pytest.raises(ValueError):
        PlanTier(name="X", max_traces_per_period=1, max_features=1, rank=-1)
    with pytest.raises(ValueError):
        PlanTier(name="X", max_traces_per_period=1, max_features=1, price_micro_usd=-1)


def test_unlimited_meter_allowed() -> None:
    tier = PlanTier(name="U", max_traces_per_period=None, max_features=5)
    assert tier.is_unlimited(Meter.TRACES)
    assert not tier.is_unlimited(Meter.FEATURES)


def test_plan_tier_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        FREE.name = "MUTATED"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# UsageSnapshot
# --------------------------------------------------------------------------- #
def test_usage_clamps_negatives() -> None:
    u = UsageSnapshot(traces=-5, features=-3)
    assert u.traces == 0
    assert u.features == 0


def test_usage_value_for() -> None:
    u = UsageSnapshot(traces=42, features=7)
    assert u.value_for(Meter.TRACES) == 42
    assert u.value_for(Meter.FEATURES) == 7


# --------------------------------------------------------------------------- #
# Enforcement levels at the boundaries (cap = 100 traces on a custom tier)
# --------------------------------------------------------------------------- #
@pytest.fixture
def cap100() -> PlanTier:
    return PlanTier(name="CAP100", max_traces_per_period=100, max_features=1000)


@pytest.mark.parametrize(
    "traces,expected",
    [
        (0, EnforcementLevel.OK),
        (79, EnforcementLevel.OK),  # just under 80%
        (80, EnforcementLevel.WARN),  # exactly 80%
        (99, EnforcementLevel.WARN),  # just under 100%
        (100, EnforcementLevel.SOFT_CAP),  # exactly at cap
        (149, EnforcementLevel.SOFT_CAP),  # over but under 150%
        (150, EnforcementLevel.UPGRADE_REQUIRED),  # 150% of cap
        (1000, EnforcementLevel.UPGRADE_REQUIRED),  # way over
    ],
)
def test_trace_boundaries(cap100: PlanTier, traces: int, expected: EnforcementLevel) -> None:
    decision = classify(cap100, UsageSnapshot(traces=traces, features=0))
    assert decision.level is expected


def test_warn_at_eighty_percent_carries_numbers(cap100: PlanTier) -> None:
    d = classify(cap100, UsageSnapshot(traces=80, features=0))
    assert d.meter is Meter.TRACES
    assert d.usage == 80
    assert d.limit == 100
    assert d.percent_used == pytest.approx(80.0)
    assert "upgrad" in d.message.lower()


def test_soft_cap_message_promises_data_kept(cap100: PlanTier) -> None:
    d = classify(cap100, UsageSnapshot(traces=120, features=0))
    assert d.level is EnforcementLevel.SOFT_CAP
    assert "still accepting" in d.message.lower()


# --------------------------------------------------------------------------- #
# The never-drops-data invariant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("traces", [0, 80, 100, 200, 10_000_000])
def test_decision_never_drops_data(cap100: PlanTier, traces: int) -> None:
    d = classify(cap100, UsageSnapshot(traces=traces, features=0))
    assert d.drops_data is False


def test_limit_state_never_drops_data(cap100: PlanTier) -> None:
    state = build_limit_state(cap100, UsageSnapshot(traces=99999, features=99999))
    assert state.drops_data is False


def test_every_level_reports_no_drop() -> None:
    tier = PlanTier(name="T", max_traces_per_period=10, max_features=10)
    for traces in (0, 8, 10, 20):
        assert classify(tier, UsageSnapshot(traces=traces, features=0)).drops_data is False


# --------------------------------------------------------------------------- #
# Both meters + which-meter-tripped
# --------------------------------------------------------------------------- #
def test_features_meter_can_trip() -> None:
    tier = PlanTier(name="T", max_traces_per_period=1_000_000, max_features=5)
    d = classify(tier, UsageSnapshot(traces=0, features=6))
    assert d.meter is Meter.FEATURES
    assert d.level is EnforcementLevel.SOFT_CAP


def test_worst_meter_wins_when_both_exceed() -> None:
    # traces at SOFT_CAP, features at UPGRADE_REQUIRED -> features should win.
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    d = classify(tier, UsageSnapshot(traces=110, features=100))
    assert d.level is EnforcementLevel.UPGRADE_REQUIRED
    assert d.meter is Meter.FEATURES


def test_tie_breaks_to_traces() -> None:
    # both meters at SOFT_CAP -> TRACES (billable unit) reported.
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    d = classify(tier, UsageSnapshot(traces=100, features=10))
    assert d.level is EnforcementLevel.SOFT_CAP
    assert d.meter is Meter.TRACES


def test_traces_trip_while_features_ok() -> None:
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=1000)
    d = classify(tier, UsageSnapshot(traces=200, features=1))
    assert d.meter is Meter.TRACES
    assert d.level is EnforcementLevel.UPGRADE_REQUIRED


# --------------------------------------------------------------------------- #
# Unlimited / enterprise never trips
# --------------------------------------------------------------------------- #
def test_enterprise_never_trips() -> None:
    d = classify(ENTERPRISE, UsageSnapshot(traces=10**12, features=10**6))
    assert d.level is EnforcementLevel.OK
    assert d.percent_used == 0.0
    assert d.needs_upgrade is False


def test_partial_unlimited_tier() -> None:
    tier = PlanTier(name="P", max_traces_per_period=None, max_features=4)
    over = classify(tier, UsageSnapshot(traces=10**9, features=4))
    assert over.meter is Meter.FEATURES
    assert over.level is EnforcementLevel.SOFT_CAP


def test_zero_cap_disables_meter() -> None:
    # A zero cap means the feature is off on this tier: any usage -> upgrade.
    tier = PlanTier(name="Z", max_traces_per_period=100, max_features=0)
    none_used = classify(tier, UsageSnapshot(traces=0, features=0))
    assert none_used.level is EnforcementLevel.OK
    used = classify(tier, UsageSnapshot(traces=0, features=1))
    assert used.meter is Meter.FEATURES
    assert used.level is EnforcementLevel.UPGRADE_REQUIRED
    assert used.percent_used == float("inf")


# --------------------------------------------------------------------------- #
# Next tier up
# --------------------------------------------------------------------------- #
def test_next_tier_up_from_free() -> None:
    usage = UsageSnapshot(traces=50_000, features=4)  # over FREE on both
    nxt = next_tier_up(FREE, usage)
    assert nxt is PRO


def test_next_tier_up_skips_insufficient_tier() -> None:
    # Usage clears SCALE but not PRO -> jump to SCALE.
    usage = UsageSnapshot(traces=5_000_000, features=4)
    nxt = next_tier_up(FREE, usage)
    assert nxt is SCALE


def test_next_tier_up_returns_enterprise_for_huge_usage() -> None:
    usage = UsageSnapshot(traces=10**11, features=10_000)
    nxt = next_tier_up(FREE, usage)
    assert nxt is ENTERPRISE


def test_next_tier_up_on_top_tier_returns_none() -> None:
    usage = UsageSnapshot(traces=10**9, features=500)
    assert next_tier_up(ENTERPRISE, usage) is None


def test_next_tier_up_none_when_no_higher_clears() -> None:
    # On PRO, usage that not even ENTERPRISE could fail to clear — but here
    # construct a registry where the only higher tier doesn't cover usage.
    small_top = PlanTier(name="SMALL_TOP", max_traces_per_period=10, max_features=1, rank=9)
    usage = UsageSnapshot(traces=1000, features=50)
    assert next_tier_up(PRO, usage, tiers=(PRO, small_top)) is None


# --------------------------------------------------------------------------- #
# Tier change takes effect
# --------------------------------------------------------------------------- #
def test_apply_tier_change_clears_after_upgrade() -> None:
    usage = UsageSnapshot(traces=50_000, features=4)
    before = classify(FREE, usage)
    assert before.needs_upgrade is True
    after = apply_tier_change(PRO, usage)
    assert after.level is EnforcementLevel.OK
    assert after.tier_name == "PRO"


def test_apply_tier_change_is_pure() -> None:
    usage = UsageSnapshot(traces=120, features=0)
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    d1 = apply_tier_change(tier, usage)
    d2 = apply_tier_change(tier, usage)
    assert d1 == d2


# --------------------------------------------------------------------------- #
# Snapshot / summary / as_dict
# --------------------------------------------------------------------------- #
def test_decision_as_dict_and_summary(cap100: PlanTier) -> None:
    d = classify(cap100, UsageSnapshot(traces=120, features=0))
    data = d.as_dict()
    assert data["level"] == "soft_cap"
    assert data["meter"] == "traces"
    assert data["usage"] == 120
    assert data["limit"] == 100
    assert data["drops_data"] is False
    assert data["needs_upgrade"] is True
    assert "soft_cap" in d.summary()
    assert "120/100" in d.summary()


def test_limit_state_structure() -> None:
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    state = build_limit_state(tier, UsageSnapshot(traces=90, features=11))
    assert isinstance(state, LimitState)
    assert len(state.meters) == 2
    assert state.overall.level is EnforcementLevel.SOFT_CAP  # features over
    assert state.needs_upgrade is True


def test_limit_state_as_dict_and_summary() -> None:
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    state = build_limit_state(tier, UsageSnapshot(traces=85, features=5))
    data = state.as_dict()
    assert data["tier_name"] == "T"
    assert data["drops_data"] is False
    assert len(data["meters"]) == 2  # type: ignore[arg-type]
    assert "traces 85/100" in state.summary()


def test_limit_state_summary_shows_unlimited() -> None:
    state = build_limit_state(ENTERPRISE, UsageSnapshot(traces=999, features=999))
    assert "unlimited" in state.summary()


def test_meter_state_as_dict() -> None:
    tier = PlanTier(name="T", max_traces_per_period=100, max_features=10)
    state = build_limit_state(tier, UsageSnapshot(traces=50, features=5))
    m = state.meters[0]
    d = m.as_dict()
    assert d["meter"] in ("traces", "features")
    assert "percent_used" in d


# --------------------------------------------------------------------------- #
# Immutability of results
# --------------------------------------------------------------------------- #
def test_decision_is_frozen(cap100: PlanTier) -> None:
    d = classify(cap100, UsageSnapshot(traces=10, features=0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.level = EnforcementLevel.OK  # type: ignore[misc]


def test_decision_drops_data_cannot_be_overridden(cap100: PlanTier) -> None:
    d = classify(cap100, UsageSnapshot(traces=10, features=0))
    assert isinstance(d, EnforcementDecision)
    # drops_data is a property — there is no settable attribute backing it.
    assert d.drops_data is False


# --------------------------------------------------------------------------- #
# Registry default + override
# --------------------------------------------------------------------------- #
def test_registry_default_is_free() -> None:
    reg = InMemoryPlanRegistry()
    assert reg.tier_for("tenant-a") is FREE


def test_registry_custom_default() -> None:
    reg = InMemoryPlanRegistry(default=PRO)
    assert reg.tier_for("unknown") is PRO


def test_registry_override_per_tenant() -> None:
    reg = InMemoryPlanRegistry()
    reg.set_tier("big-co", SCALE)
    assert reg.tier_for("big-co") is SCALE
    assert reg.tier_for("other") is FREE


def test_registry_rejects_empty_tenant() -> None:
    reg = InMemoryPlanRegistry()
    with pytest.raises(ValueError):
        reg.set_tier("", PRO)


def test_registry_satisfies_protocol() -> None:
    reg = InMemoryPlanRegistry()
    assert isinstance(reg, PlanRegistry)


def test_registry_integration_with_classify() -> None:
    reg = InMemoryPlanRegistry()
    reg.set_tier("t1", PlanTier(name="T1", max_traces_per_period=100, max_features=10))
    tier = reg.tier_for("t1")
    d = classify(tier, UsageSnapshot(traces=200, features=1))
    assert d.level is EnforcementLevel.UPGRADE_REQUIRED
