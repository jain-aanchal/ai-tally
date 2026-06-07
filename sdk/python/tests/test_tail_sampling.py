# SPDX-License-Identifier: Apache-2.0
"""Tail-weighted estimation sampling: tail over-indexing, mandatory includes, determinism."""

from __future__ import annotations

import pytest

from tally.tail_sampling import (
    HistoricalRun,
    TailSampleConfig,
    tail_weighted_sample,
)


def _workload(n: int, *, base: int = 100) -> list[HistoricalRun]:
    """n runs with linearly increasing cost (run i costs base*(i+1) micro-USD)."""
    return [HistoricalRun(run_id=f"r{i:04d}", cost_micro_usd=base * (i + 1)) for i in range(n)]


# --- determinism / reproducibility ---------------------------------------------------------------


def test_same_workload_and_seed_is_reproducible() -> None:
    runs = _workload(1000)
    cfg = TailSampleConfig(sample_size=180, seed=42)
    a = tail_weighted_sample(runs, cfg)
    b = tail_weighted_sample(runs, cfg)
    assert a.run_ids == b.run_ids


def test_different_seed_changes_the_random_body_selection() -> None:
    runs = _workload(1000)
    a = tail_weighted_sample(runs, TailSampleConfig(sample_size=180, seed=1))
    b = tail_weighted_sample(runs, TailSampleConfig(sample_size=180, seed=2))
    assert a.run_ids != b.run_ids  # body picks are seed-dependent


# --- over-indexing the expensive tail ------------------------------------------------------------


def test_sample_over_indexes_the_tail() -> None:
    runs = _workload(1000)  # costs 100..100000; P90 cutoff ~ run 900
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=200, tail_percentile=90.0))
    cutoff = sample.composition.tail_cost_cutoff
    in_tail = sum(1 for r in sample.runs if r.cost_micro_usd >= cutoff)
    # The tail is only 10% of the population but should be the majority of the sample.
    assert in_tail > sample.composition.total // 2


def test_composition_counts_are_consistent() -> None:
    runs = _workload(1000)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180, tail_fraction=0.7))
    c = sample.composition
    assert c.total == len(sample.runs)
    assert c.mandatory + c.tail_weighted + c.random == c.total
    assert c.total == 180


# --- mandatory inclusion -------------------------------------------------------------------------


def test_top_k_most_expensive_always_included() -> None:
    runs = _workload(1000)
    cfg = TailSampleConfig(sample_size=180, top_k_expensive=10)
    sample = tail_weighted_sample(runs, cfg)
    ids = set(sample.run_ids)
    # the 10 priciest are r0990..r0999
    for i in range(990, 1000):
        assert f"r{i:04d}" in ids
    assert sample.composition.mandatory_top_k == 10


def test_retry_loop_and_step_cap_runs_always_included() -> None:
    runs = _workload(1000)
    # mark two cheap runs as pathological — they'd never survive a cost-weighted sample otherwise.
    runs[3] = HistoricalRun("r0003", cost_micro_usd=400, hit_retry_loop=True)
    runs[7] = HistoricalRun("r0007", cost_micro_usd=800, hit_step_cap=True)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180, seed=99))
    ids = set(sample.run_ids)
    assert "r0003" in ids
    assert "r0007" in ids
    assert sample.composition.mandatory_retry_or_cap == 2


def test_pathological_run_in_top_k_not_double_counted() -> None:
    runs = _workload(50)
    # the most expensive run also hit a retry loop
    runs[49] = HistoricalRun("r0049", cost_micro_usd=5000, hit_retry_loop=True)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=50, top_k_expensive=10))
    c = sample.composition
    # r0049 counts once (as top-k), not in both buckets
    assert c.mandatory == c.mandatory_top_k + c.mandatory_retry_or_cap
    assert "r0049" in set(sample.run_ids)


# --- composition reporting -----------------------------------------------------------------------


def test_summary_string_is_human_readable() -> None:
    runs = _workload(1000)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180))
    s = sample.composition.summary()
    assert "tail-weighted" in s
    assert "random" in s
    assert "mandatory" in s


def test_as_dict_round_trips_counts() -> None:
    runs = _workload(500)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=100))
    d = sample.composition.as_dict()
    assert d["total"] == 100
    assert d["mandatory"] == d["mandatory_top_k"] + d["mandatory_retry_or_cap"]
    assert "summary" in d


# --- edge cases / never-crash --------------------------------------------------------------------


def test_empty_workload_returns_empty_sample() -> None:
    sample = tail_weighted_sample([], TailSampleConfig(sample_size=180))
    assert sample.runs == ()
    assert sample.composition.total == 0


def test_workload_smaller_than_budget_returns_everything() -> None:
    runs = _workload(25)
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180))
    assert sample.composition.total == 25
    assert set(sample.run_ids) == {r.run_id for r in runs}


def test_mandatory_overflow_keeps_most_expensive() -> None:
    runs = _workload(100)
    # every run pathological → mandatory pool is the whole workload, but budget is 10
    runs = [HistoricalRun(r.run_id, r.cost_micro_usd, hit_retry_loop=True) for r in runs]
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=10))
    assert sample.composition.total == 10
    # the 10 most expensive survive
    assert set(sample.run_ids) == {f"r{i:04d}" for i in range(90, 100)}


def test_thin_tail_pool_spills_budget_to_body_and_hits_target() -> None:
    # almost all runs identical-cheap; only a few expensive → tail pool is tiny.
    runs = [HistoricalRun(f"c{i}", cost_micro_usd=10) for i in range(500)]
    runs += [HistoricalRun(f"x{i}", cost_micro_usd=99999) for i in range(5)]
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180, tail_fraction=0.9))
    assert sample.composition.total == 180  # still reaches the budget despite a thin tail


def test_invalid_tail_fraction_raises() -> None:
    with pytest.raises(ValueError):
        TailSampleConfig(tail_fraction=1.5)


def test_invalid_percentile_raises() -> None:
    with pytest.raises(ValueError):
        TailSampleConfig(tail_percentile=120.0)


def test_non_historicalrun_entries_ignored() -> None:
    runs = _workload(50) + ["garbage", None]  # type: ignore[list-item]
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=180))
    assert sample.composition.total == 50  # only real runs counted


def test_cutoff_reported_matches_percentile() -> None:
    runs = _workload(100)  # costs 100..10000
    sample = tail_weighted_sample(runs, TailSampleConfig(sample_size=50, tail_percentile=90.0))
    # P90 nearest-rank over 100 runs → the 90th cheapest cost = 100*90 = 9000
    assert sample.composition.tail_cost_cutoff == 9000
