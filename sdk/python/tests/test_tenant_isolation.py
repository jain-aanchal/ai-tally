"""Tests for tally.tenant_isolation (CTO-30): admission, scope guard, resource groups, promotion."""

from __future__ import annotations

import pytest

from tally.tenant_isolation import (
    DEFAULT_MAX_CONCURRENT_QUERIES,
    CrossTenantAccessError,
    IsolationTier,
    QueryAdmissionError,
    QueryConcurrencyLimiter,
    ScopedQuery,
    TenantLoad,
    TenantQueryGuard,
    rank_heavy_tenants,
    recommend_shard_promotions,
    resource_group_for,
)


# --------------------------------------------------------------------------- #
# Concurrency limiter
# --------------------------------------------------------------------------- #
def test_limiter_admits_up_to_limit_then_rejects():
    lim = QueryConcurrencyLimiter()  # default 4
    outcomes = [lim.try_acquire("t1") for _ in range(DEFAULT_MAX_CONCURRENT_QUERIES)]
    assert all(o.admitted for o in outcomes)
    rejected = lim.try_acquire("t1")
    assert rejected.admitted is False
    assert rejected.in_flight == 4
    assert rejected.reason == "at concurrency limit"


def test_limiter_release_frees_a_slot():
    lim = QueryConcurrencyLimiter(max_concurrent=1)
    assert lim.try_acquire("t1").admitted is True
    assert lim.try_acquire("t1").admitted is False
    lim.release("t1")
    assert lim.try_acquire("t1").admitted is True


def test_limiter_is_per_tenant_independent():
    lim = QueryConcurrencyLimiter(max_concurrent=1)
    assert lim.try_acquire("t1").admitted is True
    # t2 is unaffected by t1 saturating its own limit
    assert lim.try_acquire("t2").admitted is True
    assert lim.try_acquire("t1").admitted is False


def test_limiter_release_never_negative():
    lim = QueryConcurrencyLimiter()
    lim.release("t1")  # no prior acquire
    assert lim.in_flight("t1") == 0


def test_limiter_per_tenant_override():
    lim = QueryConcurrencyLimiter(max_concurrent=2)
    lim.set_limit("ent", 5)
    assert lim.limit_for("ent") == 5
    assert lim.limit_for("free") == 2
    for _ in range(5):
        assert lim.try_acquire("ent").admitted is True
    assert lim.try_acquire("ent").admitted is False


def test_limiter_missing_tenant_rejected():
    lim = QueryConcurrencyLimiter()
    assert lim.try_acquire("").admitted is False


def test_limiter_rejects_bad_max():
    with pytest.raises(ValueError):
        QueryConcurrencyLimiter(0)
    with pytest.raises(ValueError):
        QueryConcurrencyLimiter(True)


def test_lease_context_manager_releases():
    lim = QueryConcurrencyLimiter(max_concurrent=1)
    with lim.lease("t1") as outcome:
        assert outcome.admitted is True
        assert lim.in_flight("t1") == 1
    assert lim.in_flight("t1") == 0  # released on exit


def test_lease_raises_when_at_limit():
    lim = QueryConcurrencyLimiter(max_concurrent=1)
    lim.try_acquire("t1")
    with pytest.raises(QueryAdmissionError):
        with lim.lease("t1"):
            pass


def test_lease_releases_even_on_exception():
    lim = QueryConcurrencyLimiter(max_concurrent=1)
    with pytest.raises(RuntimeError):
        with lim.lease("t1"):
            raise RuntimeError("boom")
    assert lim.in_flight("t1") == 0


# --------------------------------------------------------------------------- #
# Tenant-scope guard (the negative test)
# --------------------------------------------------------------------------- #
def test_query_scoped_to_own_tenant_is_safe():
    guard = TenantQueryGuard()
    q = ScopedQuery("t1", frozenset({"t1"}))
    assert guard.is_safe(q) is True
    guard.check(q)  # does not raise


def test_cross_tenant_query_is_blocked():
    guard = TenantQueryGuard()
    q = ScopedQuery("t1", frozenset({"t1", "t2"}))
    assert guard.is_safe(q) is False
    with pytest.raises(CrossTenantAccessError):
        guard.check(q)


def test_foreign_only_query_is_blocked():
    guard = TenantQueryGuard()
    q = ScopedQuery("t1", frozenset({"t2"}))
    with pytest.raises(CrossTenantAccessError):
        guard.check(q)


def test_unscoped_query_is_blocked():
    guard = TenantQueryGuard()
    q = ScopedQuery("t1", frozenset())  # no tenant predicate -> whole-cluster scan
    assert guard.violation(q) == "query is not tenant-scoped (no tenant predicate)"
    with pytest.raises(CrossTenantAccessError):
        guard.check(q)


def test_scoped_query_requires_requester():
    with pytest.raises(ValueError):
        ScopedQuery("", frozenset({"t1"}))


# --------------------------------------------------------------------------- #
# Resource groups
# --------------------------------------------------------------------------- #
def test_resource_group_tiers_scale_up():
    free = resource_group_for("t1", IsolationTier.FREE)
    pro = resource_group_for("t1", IsolationTier.PRO)
    ent = resource_group_for("t1", IsolationTier.ENTERPRISE)
    assert free.max_memory_bytes < pro.max_memory_bytes < ent.max_memory_bytes
    assert free.max_concurrent_queries < ent.max_concurrent_queries


def test_resource_group_default_tier_is_pro():
    rg = resource_group_for("t1")
    assert rg.max_concurrent_queries == 4


def test_resource_group_as_settings():
    rg = resource_group_for("acme", IsolationTier.ENTERPRISE)
    s = rg.as_settings()
    assert s["profile"] == "tenant_acme"
    assert s["max_concurrent_queries_for_user"] == 16
    assert s["max_memory_usage"] == 32 * (1024**3)


def test_resource_group_rejects_empty_tenant():
    with pytest.raises(ValueError):
        resource_group_for("", IsolationTier.PRO)


# --------------------------------------------------------------------------- #
# Shard promotion
# --------------------------------------------------------------------------- #
def _load(tid, gib_per_day, qpd=0):
    return TenantLoad(tid, queries_per_day=qpd, bytes_scanned_per_day=gib_per_day * (1024**3))


def test_rank_heavy_tenants_orders_by_load():
    loads = [_load("small", 10), _load("huge", 9000), _load("mid", 1000)]
    ranked = rank_heavy_tenants(loads)
    assert [t.tenant_id for t in ranked] == ["huge", "mid", "small"]


def test_rank_top_n_limit():
    loads = [_load(f"t{i}", i * 100) for i in range(1, 6)]
    assert len(rank_heavy_tenants(loads, top_n=2)) == 2


def test_recommend_shard_promotions_threshold():
    loads = [_load("light", 100), _load("heavy", 9000)]
    # default threshold ~5 TiB/day -> only 'heavy' qualifies
    assert recommend_shard_promotions(loads) == ("heavy",)


def test_recommend_none_below_threshold():
    loads = [_load("a", 50), _load("b", 100)]
    assert recommend_shard_promotions(loads) == ()


def test_load_score_breaks_ties_on_query_rate():
    a = _load("a", 1000, qpd=5000)
    b = _load("b", 1000, qpd=1000)
    ranked = rank_heavy_tenants([b, a])
    assert ranked[0].tenant_id == "a"  # same scan volume, more queries -> heavier
