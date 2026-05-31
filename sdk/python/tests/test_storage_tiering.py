"""Tests for tally.storage_tiering (CTO-29): tier classification + TTL DDL generation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tally.storage_tiering import (
    DEFAULT_POLICY,
    WARM_AGGREGATE_DIMENSIONS,
    InMemoryTieringPolicyStore,
    StorageTier,
    TieringPolicy,
    TtlAction,
    TtlActionKind,
    render_tenant_ttl_delete_expression,
)

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# TieringPolicy validation
# --------------------------------------------------------------------------- #
def test_policy_defaults_are_canonical():
    p = DEFAULT_POLICY
    assert (p.hot_days, p.warm_days, p.cold_days) == (7, 30, 90)


def test_policy_rejects_bad_ordering():
    with pytest.raises(ValueError):
        TieringPolicy(hot_days=30, warm_days=7, cold_days=90)
    with pytest.raises(ValueError):
        TieringPolicy(hot_days=7, warm_days=90, cold_days=30)


def test_policy_rejects_non_positive_and_bool():
    with pytest.raises(ValueError):
        TieringPolicy(hot_days=0)
    with pytest.raises(ValueError):
        TieringPolicy(hot_days=-1)
    with pytest.raises(ValueError):
        TieringPolicy(hot_days=True)  # bool is not a valid int day count


def test_policy_rejects_empty_volume():
    with pytest.raises(ValueError):
        TieringPolicy(warm_volume="")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "days,expected",
    [
        (0, StorageTier.HOT),
        (6.9, StorageTier.HOT),
        (7, StorageTier.WARM),
        (29.9, StorageTier.WARM),
        (30, StorageTier.COLD),
        (89.9, StorageTier.COLD),
        (90, StorageTier.AGGREGATE),
        (365, StorageTier.AGGREGATE),
    ],
)
def test_tier_for_age_boundaries(days, expected):
    assert DEFAULT_POLICY.tier_for_age(timedelta(days=days)) is expected


def test_negative_age_clock_skew_is_hot():
    assert DEFAULT_POLICY.tier_for_age(timedelta(days=-2)) is StorageTier.HOT


def test_tier_at_uses_as_of():
    span = datetime(2026, 1, 1, tzinfo=UTC)
    assert DEFAULT_POLICY.tier_at(span, datetime(2026, 1, 3, tzinfo=UTC)) is StorageTier.HOT
    assert DEFAULT_POLICY.tier_at(span, datetime(2026, 1, 20, tzinfo=UTC)) is StorageTier.WARM
    assert DEFAULT_POLICY.tier_at(span, datetime(2026, 2, 15, tzinfo=UTC)) is StorageTier.COLD
    assert DEFAULT_POLICY.tier_at(span, datetime(2026, 6, 1, tzinfo=UTC)) is StorageTier.AGGREGATE


def test_tier_at_coerces_naive_to_utc():
    span = datetime(2026, 1, 1)  # naive
    as_of = datetime(2026, 1, 2)
    assert DEFAULT_POLICY.tier_at(span, as_of) is StorageTier.HOT


def test_raw_dropped_only_past_cold_horizon():
    span = datetime(2026, 1, 1, tzinfo=UTC)
    assert DEFAULT_POLICY.raw_dropped(span, datetime(2026, 2, 1, tzinfo=UTC)) is False
    assert DEFAULT_POLICY.raw_dropped(span, datetime(2026, 5, 1, tzinfo=UTC)) is True


# --------------------------------------------------------------------------- #
# TTL DDL generation
# --------------------------------------------------------------------------- #
def test_ttl_actions_order_and_targets():
    actions = DEFAULT_POLICY.ttl_actions()
    assert actions == (
        TtlAction(7, TtlActionKind.MOVE, "warm"),
        TtlAction(30, TtlActionKind.MOVE, "cold"),
        TtlAction(90, TtlActionKind.DELETE),
    )


def test_ttl_action_sql():
    assert TtlAction(7, TtlActionKind.MOVE, "warm").to_sql() == (
        "toDateTime(Timestamp) + INTERVAL 7 DAY TO VOLUME 'warm'"
    )
    assert TtlAction(90, TtlActionKind.DELETE).to_sql() == (
        "toDateTime(Timestamp) + INTERVAL 90 DAY DELETE"
    )


def test_render_ttl_clause_matches_policy():
    clause = DEFAULT_POLICY.render_ttl_clause()
    assert clause == (
        "TTL\n"
        "    toDateTime(Timestamp) + INTERVAL 7 DAY TO VOLUME 'warm',\n"
        "    toDateTime(Timestamp) + INTERVAL 30 DAY TO VOLUME 'cold',\n"
        "    toDateTime(Timestamp) + INTERVAL 90 DAY DELETE"
    )


def test_render_ttl_clause_custom_column():
    clause = TieringPolicy(hot_days=1, warm_days=2, cold_days=3).render_ttl_clause(
        timestamp_column="EventTs"
    )
    assert "toDateTime(EventTs) + INTERVAL 1 DAY TO VOLUME 'warm'" in clause


# --------------------------------------------------------------------------- #
# Per-tenant overrides
# --------------------------------------------------------------------------- #
def test_store_returns_default_when_no_override():
    store = InMemoryTieringPolicyStore()
    assert store.policy_for("t1") is DEFAULT_POLICY


def test_store_override_extends_retention():
    store = InMemoryTieringPolicyStore()
    enterprise = TieringPolicy(hot_days=7, warm_days=30, cold_days=365)
    store.set_override("ent", enterprise)
    assert store.policy_for("ent") is enterprise
    assert store.policy_for("free") is DEFAULT_POLICY
    # An old span (120 days) is still raw for the enterprise tenant (cold_days=365 -> COLD),
    # but dropped to aggregate-only for a default-policy tenant (cold_days=90).
    span = datetime(2026, 1, 1, tzinfo=UTC)
    as_of = datetime(2026, 5, 1, tzinfo=UTC)  # 120 days later
    assert store.tier_at("ent", span, as_of) is StorageTier.COLD
    assert store.tier_at("free", span, as_of) is StorageTier.AGGREGATE


def test_store_set_override_rejects_empty_tenant():
    store = InMemoryTieringPolicyStore()
    with pytest.raises(ValueError):
        store.set_override("", DEFAULT_POLICY)


def test_tenant_ttl_expression_default_only():
    store = InMemoryTieringPolicyStore()
    expr = render_tenant_ttl_delete_expression(store)
    assert expr == "toDateTime(Timestamp) + INTERVAL 90 DAY DELETE"


def test_tenant_ttl_expression_with_overrides_is_deterministic():
    store = InMemoryTieringPolicyStore()
    store.set_override("zeta", TieringPolicy(hot_days=7, warm_days=30, cold_days=365))
    store.set_override("alpha", TieringPolicy(hot_days=7, warm_days=30, cold_days=180))
    expr = render_tenant_ttl_delete_expression(store)
    # overrides sorted by tenant id, default last as fallback
    assert expr == (
        "toDateTime(Timestamp) + multiIf("
        "TenantId = 'alpha', INTERVAL 180 DAY, "
        "TenantId = 'zeta', INTERVAL 365 DAY, "
        "INTERVAL 90 DAY) DELETE"
    )


# --------------------------------------------------------------------------- #
# Aggregate survivability contract
# --------------------------------------------------------------------------- #
def test_warm_aggregate_dimensions_match_rollup_key():
    # The surviving aggregate must be keyed by exactly these dims (spec §5.1 / rollup).
    assert WARM_AGGREGATE_DIMENSIONS == ("TenantId", "FeatureTag", "Day", "GenAiResponseModel")
