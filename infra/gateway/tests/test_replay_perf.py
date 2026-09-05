# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the replay/eval hot-path performance primitives (CTO-243)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from gateway.replay_perf import (
    EnvelopeCache,
    ProjectionCache,
    ReplayRunStore,
    candidates_key,
    prime_envelopes,
)


@dataclass
class _Row:
    tenant_id: str
    sample_id: str
    candidate_provider: str
    candidate_model: str
    cost_micro_usd: int = 0
    ran_at: datetime = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _row(tenant: str, sample: str, model: str, cost: int = 0) -> _Row:
    return _Row(tenant, sample, "openai", model, cost)


def test_run_store_dedupes_by_sample_and_candidate_newest_wins() -> None:
    store = ReplayRunStore()
    # Same (tenant, sample, candidate) appended three times must collapse to ONE, keeping the last.
    store.append(_row("t1", "s1", "gpt-5-mini", cost=10))
    store.append(_row("t1", "s1", "gpt-5-mini", cost=20))
    store.append(_row("t1", "s1", "gpt-5-mini", cost=30))
    rows = list(store)
    assert len(store) == 1
    assert len(rows) == 1
    assert rows[0].cost_micro_usd == 30


def test_run_store_keeps_distinct_pairs() -> None:
    store = ReplayRunStore()
    store.append(_row("t1", "s1", "gpt-5-mini"))
    store.append(_row("t1", "s1", "claude-haiku-4-5"))  # different candidate
    store.append(_row("t1", "s2", "gpt-5-mini"))  # different sample
    store.append(_row("t2", "s1", "gpt-5-mini"))  # different tenant
    assert len(store) == 4


def test_run_store_caps_per_tenant() -> None:
    store = ReplayRunStore(per_tenant_cap=3)
    for i in range(10):
        store.append(_row("t1", f"s{i}", "gpt-5-mini"))
    # One tenant, 10 distinct samples, cap 3 → only the newest 3 survive.
    assert len(store) == 3
    surviving = {r.sample_id for r in store}
    assert surviving == {"s7", "s8", "s9"}


def test_envelope_cache_lru_eviction_and_hits() -> None:
    cache = EnvelopeCache(max_size=2)
    cache.put("a", {"v": 1})
    cache.put("b", {"v": 2})
    assert cache.get("a") == {"v": 1}  # touch a so b is now the LRU
    cache.put("c", {"v": 3})  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") == {"v": 1}
    assert cache.get("c") == {"v": 3}


def test_prime_envelopes_loads_misses_once_and_skips_errors() -> None:
    cache = EnvelopeCache()
    calls: list[str] = []

    def loader(key: str) -> dict:
        calls.append(key)
        if key == "bad":
            raise RuntimeError("unreadable body")
        return {"key": key}

    prime_envelopes(cache, {"a", "b", "bad"}, loader)
    assert cache.get("a") == {"key": "a"}
    assert cache.get("b") == {"key": "b"}
    assert cache.get("bad") is None  # a bad body is skipped, not fatal
    # Priming again loads only the still-missing key, not the already-cached ones.
    calls.clear()
    prime_envelopes(cache, {"a", "b", "bad"}, loader)
    assert calls == ["bad"]


def test_projection_cache_signature_and_ttl() -> None:
    cache = ProjectionCache(ttl_s=1000)
    key = ("eval", "t1", "chatbot", 50, candidates_key([{"provider": "openai", "model": "m"}]))
    cache.put(key, "sig-1", {"result": 1})
    assert cache.get(key, "sig-1") == {"result": 1}
    # A changed corpus signature invalidates the entry (returns None, forcing recompute).
    assert cache.get(key, "sig-2") is None


def test_projection_cache_expires() -> None:
    cache = ProjectionCache(ttl_s=0)  # everything is immediately stale
    key = ("replay", "t1", "", 50, ())
    cache.put(key, "sig", {"r": 1})
    assert cache.get(key, "sig") is None


def test_candidates_key_is_order_independent() -> None:
    a = candidates_key([{"provider": "openai", "model": "x"}, {"provider": "anthropic", "model": "y"}])
    b = candidates_key([{"provider": "anthropic", "model": "y"}, {"provider": "openai", "model": "x"}])
    assert a == b
