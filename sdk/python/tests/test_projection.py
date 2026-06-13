# SPDX-License-Identifier: Apache-2.0
"""Projection engine: p99 headline, blow-up risk + CI, driver breakdown, determinism (CTO-72)."""

from __future__ import annotations

import pytest

from tally.projection import (
    BlowupRisk,
    CostStat,
    DriverContribution,
    ProjectionConfig,
    ProjectionReport,
    ProjectionRun,
    project,
)


def _flat(n: int, *, baseline: int, projected: int, weight: float = 1.0) -> list[ProjectionRun]:
    """n identical runs (same baseline + projected cost)."""
    return [
        ProjectionRun(
            run_id=f"r{i:04d}",
            baseline_cost_micro_usd=baseline,
            projected_cost_micro_usd=projected,
            population_weight=weight,
        )
        for i in range(n)
    ]


def _tail_workload() -> list[ProjectionRun]:
    """490 cheap runs + 10 monsters (top 2%); the change inflates only the monster tail.

    The monsters are >1% of the population so they land at/above the nearest-rank p99, while the
    mean stays small — exactly the case where a mean-only estimate would miss the blow-up.
    """
    runs = [
        ProjectionRun(f"c{i}", baseline_cost_micro_usd=1_000, projected_cost_micro_usd=1_000)
        for i in range(490)
    ]
    runs += [
        ProjectionRun(f"m{i}", baseline_cost_micro_usd=50_000, projected_cost_micro_usd=200_000)
        for i in range(10)
    ]
    return runs


# --- determinism -------------------------------------------------------------------------------


def test_same_input_and_seed_is_reproducible() -> None:
    runs = _tail_workload()
    cfg = ProjectionConfig(seed=7, bootstrap_iterations=500)
    a = project(runs, cfg)
    b = project(runs, cfg)
    assert a.as_dict() == b.as_dict()


def test_different_seed_can_shift_the_interval() -> None:
    runs = _tail_workload()
    a = project(runs, ProjectionConfig(seed=1, bootstrap_iterations=300))
    b = project(runs, ProjectionConfig(seed=2, bootstrap_iterations=300))
    # Point estimates are identical; the bootstrap CI is what depends on the seed.
    assert a.headline_projected_micro_usd == b.headline_projected_micro_usd
    a_ci = (a.blowup_risk.ci_low, a.blowup_risk.ci_high)
    b_ci = (b.blowup_risk.ci_low, b.blowup_risk.ci_high)
    # probability point estimate may differ slightly across seeds; intervals are valid either way
    assert 0.0 <= a.blowup_risk.probability <= 1.0
    assert a_ci[0] <= a.blowup_risk.probability <= a_ci[1]
    assert b_ci[0] <= b.blowup_risk.probability <= b_ci[1]


# --- p99 headline ------------------------------------------------------------------------------


def test_headline_is_projected_p99_not_mean() -> None:
    runs = _tail_workload()
    report = project(runs, ProjectionConfig(bootstrap_iterations=200))
    # The mean barely moves (one run in 100), but the p99 headline captures the tail blow-up.
    assert report.headline_projected_micro_usd == 200_000
    assert report.projected_mean_micro_usd < 5_000  # mean stays small
    assert report.headline_projected_micro_usd > report.projected_mean_micro_usd * 10


def test_headline_percentile_is_configurable() -> None:
    runs = _tail_workload()
    report = project(runs, ProjectionConfig(headline_percentile=50.0, bootstrap_iterations=50))
    # at the median the monster is invisible
    assert report.headline_percentile == 50.0
    assert report.headline_projected_micro_usd == 1_000


def test_population_weight_reexpands_the_tail() -> None:
    # A tail-weighted sample: the monster is over-sampled (weight 1) while each cheap run stands
    # for 50 population runs (weight 50). Re-expanded, p99 should still be cheap, not the monster.
    runs = [
        ProjectionRun(f"c{i}", baseline_cost_micro_usd=1_000, projected_cost_micro_usd=1_000,
                      population_weight=50.0)
        for i in range(10)
    ]
    runs.append(
        ProjectionRun("monster", baseline_cost_micro_usd=50_000, projected_cost_micro_usd=200_000,
                      population_weight=1.0)
    )
    report = project(runs, ProjectionConfig(bootstrap_iterations=100))
    # monster is ~1/501 of the population → below p99 → headline stays cheap
    assert report.headline_projected_micro_usd == 1_000
    assert report.effective_population == pytest.approx(501.0)


# --- blow-up risk ------------------------------------------------------------------------------


def test_blowup_risk_high_when_tail_more_than_doubles() -> None:
    runs = _tail_workload()  # p99 goes 50k -> 200k = 4x
    report = project(runs, ProjectionConfig(blowup_multiple=2.0, bootstrap_iterations=500))
    risk = report.blowup_risk
    assert risk.probability > 0.5
    assert risk.baseline_p99_micro_usd == 50_000
    assert risk.projected_p99_micro_usd == 200_000
    assert 0.0 <= risk.ci_low <= risk.probability <= risk.ci_high <= 1.0


def test_blowup_risk_low_when_change_is_flat() -> None:
    runs = _flat(100, baseline=1_000, projected=1_050)  # +5%, nowhere near 2x
    report = project(runs, ProjectionConfig(blowup_multiple=2.0, bootstrap_iterations=300))
    assert report.blowup_risk.probability == 0.0


def test_p99_interval_is_wider_than_median_interval() -> None:
    # Heavy-tailed (power-law) costs: a dense cheap body + a sparse, widely-spread expensive tail.
    # This is the case the feature targets — the p99 estimate is genuinely noisier than the median.
    body = [
        ProjectionRun(f"b{i}", baseline_cost_micro_usd=1_000 + (i % 5),
                      projected_cost_micro_usd=1_000 + (i % 5))
        for i in range(180)
    ]
    tail = [
        ProjectionRun(f"t{i}", baseline_cost_micro_usd=10_000 * (i + 1),
                      projected_cost_micro_usd=10_000 * (i + 1))
        for i in range(20)
    ]
    cfg = ProjectionConfig(percentiles=(50.0, 99.0), bootstrap_iterations=400)
    report = project(body + tail, cfg)
    by_pct = {s.percentile: s for s in report.percentiles}
    width_50 = by_pct[50.0].ci_high_micro_usd - by_pct[50.0].ci_low_micro_usd
    width_99 = by_pct[99.0].ci_high_micro_usd - by_pct[99.0].ci_low_micro_usd
    assert width_99 > width_50


def test_zero_baseline_p99_does_not_crash_and_flags_blowup() -> None:
    runs = _flat(100, baseline=0, projected=5_000)
    report = project(runs, ProjectionConfig(bootstrap_iterations=50))
    assert report.blowup_risk.probability == 1.0  # any positive projected tail over a zero baseline


# --- driver breakdown --------------------------------------------------------------------------


def test_driver_breakdown_attributes_the_delta() -> None:
    runs = [
        ProjectionRun(
            f"r{i}",
            baseline_cost_micro_usd=1_000,
            projected_cost_micro_usd=1_000 + 380 + 140,
            drivers={"system_prompt": 380, "tool_call": 140},
        )
        for i in range(100)
    ]
    report = project(runs, ProjectionConfig(bootstrap_iterations=10))
    by_name = {d.name: d for d in report.drivers}
    assert by_name["system_prompt"].delta_micro_usd == 38_000  # 380 * 100 runs
    assert by_name["tool_call"].delta_micro_usd == 14_000
    # drivers sorted by magnitude: system_prompt first
    assert report.drivers[0].name == "system_prompt"
    assert report.residual_micro_usd == 0  # drivers fully explain the delta
    assert by_name["system_prompt"].share == pytest.approx(380 / 520)


def test_driver_residual_captures_unexplained_delta() -> None:
    runs = [
        ProjectionRun(
            f"r{i}",
            baseline_cost_micro_usd=1_000,
            projected_cost_micro_usd=2_000,  # +1000 delta
            drivers={"system_prompt": 600},  # only 600 explained
        )
        for i in range(10)
    ]
    report = project(runs, ProjectionConfig(bootstrap_iterations=10))
    assert report.residual_micro_usd == 4_000  # (1000 - 600) * 10


def test_drivers_population_weighted() -> None:
    runs = [
        ProjectionRun("a", baseline_cost_micro_usd=0, projected_cost_micro_usd=100,
                      population_weight=10.0, drivers={"x": 100}),
    ]
    report = project(runs, ProjectionConfig(bootstrap_iterations=10))
    assert report.drivers[0].delta_micro_usd == 1_000  # 100 * weight 10


# --- reporting ---------------------------------------------------------------------------------


def test_summary_leads_with_p99_and_risk() -> None:
    runs = _tail_workload()
    report = project(runs, ProjectionConfig(bootstrap_iterations=100))
    s = report.summary()
    assert "p99" in s
    assert "blow-up risk" in s


def test_as_dict_round_trips() -> None:
    runs = _tail_workload()
    d = project(runs, ProjectionConfig(bootstrap_iterations=50)).as_dict()
    assert d["headline_projected_micro_usd"] == 200_000
    assert "blowup_risk" in d
    assert "summary" in d
    assert isinstance(d["percentiles"], list)


# --- edge cases / never-crash ------------------------------------------------------------------


def test_empty_input_returns_zeroed_report() -> None:
    report = project([])
    assert isinstance(report, ProjectionReport)
    assert report.run_count == 0
    assert report.headline_projected_micro_usd == 0
    assert report.blowup_risk.probability == 0.0


def test_non_projectionrun_entries_ignored() -> None:
    runs = _flat(10, baseline=100, projected=200) + ["garbage", None]  # type: ignore[list-item]
    report = project(runs, ProjectionConfig(bootstrap_iterations=10))
    assert report.run_count == 10


def test_nonpositive_weight_runs_ignored() -> None:
    runs = [
        ProjectionRun("a", baseline_cost_micro_usd=100, projected_cost_micro_usd=200,
                      population_weight=1.0),
        ProjectionRun("b", baseline_cost_micro_usd=100, projected_cost_micro_usd=200,
                      population_weight=0.0),
        ProjectionRun("c", baseline_cost_micro_usd=100, projected_cost_micro_usd=200,
                      population_weight=-1.0),
    ]
    report = project(runs, ProjectionConfig(bootstrap_iterations=10))
    assert report.run_count == 1


def test_single_run_does_not_crash() -> None:
    runs = [ProjectionRun("solo", 1_000, 3_000)]
    report = project(runs, ProjectionConfig(bootstrap_iterations=20))
    assert report.headline_projected_micro_usd == 3_000
    assert report.blowup_risk.baseline_p99_micro_usd == 1_000


def test_zero_bootstrap_iterations_uses_point_estimate() -> None:
    runs = _tail_workload()
    report = project(runs, ProjectionConfig(bootstrap_iterations=0))
    # no interval, just the point decision (4x > 2x → certain)
    assert report.blowup_risk.probability == 1.0
    assert report.blowup_risk.ci_low == report.blowup_risk.ci_high == 1.0


def test_types_are_exported_and_frozen() -> None:
    assert CostStat.__hash__ is not None
    assert DriverContribution.__hash__ is not None
    assert BlowupRisk.__hash__ is not None


# --- config validation -------------------------------------------------------------------------


def test_invalid_percentile_raises() -> None:
    with pytest.raises(ValueError):
        ProjectionConfig(percentiles=(50.0, 120.0))


def test_invalid_headline_percentile_raises() -> None:
    with pytest.raises(ValueError):
        ProjectionConfig(headline_percentile=-1.0)


def test_invalid_blowup_multiple_raises() -> None:
    with pytest.raises(ValueError):
        ProjectionConfig(blowup_multiple=0.0)


def test_invalid_confidence_raises() -> None:
    with pytest.raises(ValueError):
        ProjectionConfig(confidence=1.0)


def test_invalid_bootstrap_iterations_raises() -> None:
    with pytest.raises(ValueError):
        ProjectionConfig(bootstrap_iterations=-1)
