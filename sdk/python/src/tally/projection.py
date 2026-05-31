"""Projection engine + blow-up risk score for pre-deploy estimation (CTO-72, spec §9 W3).

Pre-deploy estimation answers "if I ship this prompt/model change, what happens to cost?" The
honest headline is **not** the mean — a change can leave the average flat while fattening the tail
until one run in a hundred melts the budget. So this engine projects the **p99 cost per run** as the
headline number and reports the probability the tail *blows up*.

Inputs are per-run results of replaying a proposed change over a tail-weighted sample (the sample is
CTO-71; the replay that produces the candidate costs is CTO-59 — both out of scope here). Each
:class:`ProjectionRun` carries the run's baseline cost, its projected (replayed) cost, a
``population_weight`` (how many full-traffic runs this sampled run stands for, so a tail-weighted
sample can be re-expanded to the true population), and an optional per-driver delta attribution.

From those the engine computes, all deterministically (seeded bootstrap):

* **Headline** = projected p99 cost/run (configurable); mean is reported but secondary.
* **Blow-up risk** = ``P(projected p99 > blowup_multiple x baseline p99)`` with a confidence
  interval — estimated by resampling the observed runs, so the interval widens exactly where the
  data is thin (the p99 tail).
* **Driver breakdown** attributing the projected delta to named cost drivers (e.g. longer system
  prompt, an added tool call), with a residual for whatever the named drivers do not explain.

Pure logic: no infra, no network, never raises on empty/degenerate input. Money is integer micro-USD
throughout (mirrors :mod:`tally.pricing` / :mod:`tally.schema`); never float dollars.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist

from tally.schema import micro_to_usd

__all__ = [
    "ProjectionRun",
    "ProjectionConfig",
    "CostStat",
    "DriverContribution",
    "BlowupRisk",
    "ProjectionReport",
    "project",
]


@dataclass(frozen=True, slots=True)
class ProjectionRun:
    """One replayed run: baseline cost, projected cost under the proposed change, and its weight.

    ``population_weight`` is how many full-traffic runs this sampled run represents (the inverse of
    its inclusion probability). A tail-weighted sample over-indexes the expensive end, so weights
    are what let the projection recover unbiased *population* percentiles. ``drivers`` maps a driver
    name to its signed contribution (micro-USD) to this run's projected delta — they should sum to
    ``projected_cost_micro_usd - baseline_cost_micro_usd`` (any gap becomes the report residual).
    """

    run_id: str
    baseline_cost_micro_usd: int = 0
    projected_cost_micro_usd: int = 0
    population_weight: float = 1.0
    drivers: Mapping[str, int] = field(default_factory=dict)

    @property
    def delta_micro_usd(self) -> int:
        return self.projected_cost_micro_usd - self.baseline_cost_micro_usd


@dataclass(frozen=True, slots=True)
class ProjectionConfig:
    """Projection knobs.

    ``percentiles`` are the cost percentiles reported; ``headline_percentile`` is the one promoted
    to the headline (p99 by default). ``blowup_multiple`` is the tail-growth factor the risk asks
    about (2x by default). ``bootstrap_iterations`` resamples drive the confidence intervals;
    ``confidence`` is the two-sided interval level. ``seed`` makes the bootstrap reproducible.
    """

    percentiles: tuple[float, ...] = (50.0, 90.0, 99.0)
    headline_percentile: float = 99.0
    blowup_multiple: float = 2.0
    bootstrap_iterations: int = 1000
    confidence: float = 0.90
    seed: int = 0

    def __post_init__(self) -> None:
        for p in self.percentiles:
            if not (0.0 <= p <= 100.0):
                raise ValueError(f"percentile must be in [0, 100], got {p}")
        if not (0.0 <= self.headline_percentile <= 100.0):
            raise ValueError(
                f"headline_percentile must be in [0, 100], got {self.headline_percentile}"
            )
        if self.blowup_multiple <= 0:
            raise ValueError(f"blowup_multiple must be > 0, got {self.blowup_multiple}")
        if self.bootstrap_iterations < 0:
            raise ValueError(f"bootstrap_iterations must be >= 0, got {self.bootstrap_iterations}")
        if not (0.0 < self.confidence < 1.0):
            raise ValueError(f"confidence must be in (0, 1), got {self.confidence}")


@dataclass(frozen=True, slots=True)
class CostStat:
    """Baseline vs. projected cost at one percentile, with a bootstrap CI on the projected value."""

    percentile: float
    baseline_micro_usd: int
    projected_micro_usd: int
    ci_low_micro_usd: int
    ci_high_micro_usd: int

    @property
    def delta_micro_usd(self) -> int:
        return self.projected_micro_usd - self.baseline_micro_usd

    def as_dict(self) -> dict[str, object]:
        return {
            "percentile": self.percentile,
            "baseline_micro_usd": self.baseline_micro_usd,
            "projected_micro_usd": self.projected_micro_usd,
            "delta_micro_usd": self.delta_micro_usd,
            "ci_low_micro_usd": self.ci_low_micro_usd,
            "ci_high_micro_usd": self.ci_high_micro_usd,
        }


@dataclass(frozen=True, slots=True)
class DriverContribution:
    """One named cost driver's share of the projected delta (micro-USD)."""

    name: str
    delta_micro_usd: int
    share: float  # fraction of the total projected delta, in [-inf, inf] (guarded to 0 if delta==0)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "delta_micro_usd": self.delta_micro_usd, "share": self.share}


@dataclass(frozen=True, slots=True)
class BlowupRisk:
    """Probability the projected tail blows up past ``multiple`` x the baseline tail, with a CI."""

    multiple: float
    probability: float
    ci_low: float
    ci_high: float
    baseline_p99_micro_usd: int
    projected_p99_micro_usd: int

    def as_dict(self) -> dict[str, object]:
        return {
            "multiple": self.multiple,
            "probability": self.probability,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "baseline_p99_micro_usd": self.baseline_p99_micro_usd,
            "projected_p99_micro_usd": self.projected_p99_micro_usd,
        }


@dataclass(frozen=True, slots=True)
class ProjectionReport:
    """The full projection: headline p99, mean, per-percentile stats, blow-up risk, drivers."""

    run_count: int
    effective_population: float
    headline_percentile: float
    headline_projected_micro_usd: int
    baseline_mean_micro_usd: int
    projected_mean_micro_usd: int
    percentiles: tuple[CostStat, ...]
    blowup_risk: BlowupRisk
    drivers: tuple[DriverContribution, ...]
    residual_micro_usd: int

    @property
    def projected_delta_micro_usd(self) -> int:
        return self.projected_mean_micro_usd - self.baseline_mean_micro_usd

    def summary(self) -> str:
        """One-line human summary leading with the p99 headline and the blow-up risk."""
        head = micro_to_usd(self.headline_projected_micro_usd)
        risk = self.blowup_risk
        return (
            f"projected p{self.headline_percentile:g} cost/run ${head} "
            f"(blow-up risk P(>{risk.multiple:g}x)={risk.probability:.0%}, "
            f"CI {risk.ci_low:.0%}-{risk.ci_high:.0%})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "run_count": self.run_count,
            "effective_population": self.effective_population,
            "headline_percentile": self.headline_percentile,
            "headline_projected_micro_usd": self.headline_projected_micro_usd,
            "baseline_mean_micro_usd": self.baseline_mean_micro_usd,
            "projected_mean_micro_usd": self.projected_mean_micro_usd,
            "projected_delta_micro_usd": self.projected_delta_micro_usd,
            "percentiles": [s.as_dict() for s in self.percentiles],
            "blowup_risk": self.blowup_risk.as_dict(),
            "drivers": [d.as_dict() for d in self.drivers],
            "residual_micro_usd": self.residual_micro_usd,
            "summary": self.summary(),
        }


def _weighted_percentile(pairs: Sequence[tuple[int, float]], percentile: float) -> int:
    """Nearest-rank weighted percentile over ``(value, weight)`` pairs. Returns an observed value.

    Weights re-expand a tail-weighted sample to the population. Empty / all-zero-weight → 0.
    """
    items = [(v, w) for v, w in pairs if w > 0]
    if not items:
        return 0
    if percentile <= 0:
        return min(v for v, _ in items)
    if percentile >= 100:
        return max(v for v, _ in items)
    items.sort(key=lambda t: t[0])
    total = sum(w for _, w in items)
    target = percentile / 100.0 * total
    cum = 0.0
    for v, w in items:
        cum += w
        if cum >= target:
            return v
    return items[-1][0]


def _weighted_mean(pairs: Sequence[tuple[int, float]]) -> int:
    items = [(v, w) for v, w in pairs if w > 0]
    total_w = sum(w for _, w in items)
    if total_w <= 0:
        return 0
    return round(sum(v * w for v, w in items) / total_w)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile over an ascending list. ``q`` in [0, 1]. Empty → 0.0."""
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    rank = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[rank]


def _wilson_interval(successes: int, n: int, z: float) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion — well-behaved at p near 0/1 and small n."""
    if n <= 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, center - margin), min(1.0, center + margin))


def _coerce_runs(runs: Iterable[object]) -> list[ProjectionRun]:
    """Keep only real ProjectionRun entries with usable (positive) weight."""
    out: list[ProjectionRun] = []
    for r in runs:
        if isinstance(r, ProjectionRun) and r.population_weight > 0:
            out.append(r)
    return out


def _empty_report(config: ProjectionConfig) -> ProjectionReport:
    stats = tuple(
        CostStat(percentile=p, baseline_micro_usd=0, projected_micro_usd=0,
                 ci_low_micro_usd=0, ci_high_micro_usd=0)
        for p in config.percentiles
    )
    risk = BlowupRisk(
        multiple=config.blowup_multiple, probability=0.0, ci_low=0.0, ci_high=0.0,
        baseline_p99_micro_usd=0, projected_p99_micro_usd=0,
    )
    return ProjectionReport(
        run_count=0, effective_population=0.0, headline_percentile=config.headline_percentile,
        headline_projected_micro_usd=0, baseline_mean_micro_usd=0, projected_mean_micro_usd=0,
        percentiles=stats, blowup_risk=risk, drivers=(), residual_micro_usd=0,
    )


def _blowup_exceeds(projected_p99: int, baseline_p99: int, multiple: float) -> bool:
    """Did the projected tail exceed ``multiple`` x the baseline tail? Guards a zero baseline."""
    if baseline_p99 <= 0:
        return projected_p99 > 0
    return projected_p99 > multiple * baseline_p99


def _driver_breakdown(
    runs: Sequence[ProjectionRun], total_delta: float
) -> tuple[tuple[DriverContribution, ...], int]:
    """Weighted, population-scaled attribution of the projected delta to drivers + residual."""
    totals: dict[str, float] = {}
    for r in runs:
        for name, value in r.drivers.items():
            totals[name] = totals.get(name, 0.0) + value * r.population_weight
    contributions = []
    explained = 0.0
    for name, value in totals.items():
        v = round(value)
        explained += value
        share = (value / total_delta) if total_delta else 0.0
        contributions.append(DriverContribution(name=name, delta_micro_usd=v, share=share))
    contributions.sort(key=lambda d: abs(d.delta_micro_usd), reverse=True)
    residual = round(total_delta - explained)
    return tuple(contributions), residual


def project(
    runs: Iterable[ProjectionRun],
    config: ProjectionConfig | None = None,
) -> ProjectionReport:
    """Project full-traffic cost from replayed runs and score the blow-up risk.

    Steps:

    1. Re-expand the (tail-weighted) sample to the population via ``population_weight`` and compute
       weighted baseline/projected cost at each configured percentile, plus the weighted mean.
    2. Bootstrap-resample the observed runs ``bootstrap_iterations`` times (seeded) to put a
       confidence interval on every projected percentile and on the blow-up probability. For
       heavy-tailed costs the p99 interval comes out widest — that is where the data is thinnest.
    3. Attribute the projected delta to named cost drivers, with a residual for the rest.

    Never raises: empty/degenerate input yields a zeroed report.
    """
    config = config or ProjectionConfig()
    pool = _coerce_runs(runs)
    if not pool:
        return _empty_report(config)

    baseline_pairs = [(r.baseline_cost_micro_usd, r.population_weight) for r in pool]
    projected_pairs = [(r.projected_cost_micro_usd, r.population_weight) for r in pool]

    baseline_mean = _weighted_mean(baseline_pairs)
    projected_mean = _weighted_mean(projected_pairs)
    effective_population = sum(r.population_weight for r in pool)

    # Point estimates per percentile.
    point_baseline = {p: _weighted_percentile(baseline_pairs, p) for p in config.percentiles}
    point_projected = {p: _weighted_percentile(projected_pairs, p) for p in config.percentiles}
    baseline_p99 = _weighted_percentile(baseline_pairs, 99.0)
    projected_p99 = _weighted_percentile(projected_pairs, 99.0)

    # Bootstrap for CIs + blow-up probability.
    rng = random.Random(config.seed)
    n = len(pool)
    boot_projected: dict[float, list[float]] = {p: [] for p in config.percentiles}
    blowups = 0
    iters = config.bootstrap_iterations
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        b_proj = [projected_pairs[i] for i in idx]
        b_base = [baseline_pairs[i] for i in idx]
        for p in config.percentiles:
            boot_projected[p].append(_weighted_percentile(b_proj, p))
        if _blowup_exceeds(
            _weighted_percentile(b_proj, 99.0),
            _weighted_percentile(b_base, 99.0),
            config.blowup_multiple,
        ):
            blowups += 1

    z = NormalDist().inv_cdf((1.0 + config.confidence) / 2.0)
    lo_q = (1.0 - config.confidence) / 2.0
    hi_q = 1.0 - lo_q

    stats: list[CostStat] = []
    for p in config.percentiles:
        samples = sorted(boot_projected[p])
        if samples:
            ci_low = round(_quantile(samples, lo_q))
            ci_high = round(_quantile(samples, hi_q))
        else:
            ci_low = ci_high = point_projected[p]
        stats.append(
            CostStat(
                percentile=p,
                baseline_micro_usd=point_baseline[p],
                projected_micro_usd=point_projected[p],
                ci_low_micro_usd=ci_low,
                ci_high_micro_usd=ci_high,
            )
        )

    if iters > 0:
        probability = blowups / iters
        risk_lo, risk_hi = _wilson_interval(blowups, iters, z)
    else:
        # No bootstrap requested: fall back to the point estimate, no interval.
        exceeded = _blowup_exceeds(projected_p99, baseline_p99, config.blowup_multiple)
        probability = 1.0 if exceeded else 0.0
        risk_lo = risk_hi = probability

    blowup_risk = BlowupRisk(
        multiple=config.blowup_multiple,
        probability=probability,
        ci_low=risk_lo,
        ci_high=risk_hi,
        baseline_p99_micro_usd=baseline_p99,
        projected_p99_micro_usd=projected_p99,
    )

    total_delta = sum(r.delta_micro_usd * r.population_weight for r in pool)
    drivers, residual = _driver_breakdown(pool, total_delta)

    headline = _weighted_percentile(projected_pairs, config.headline_percentile)
    return ProjectionReport(
        run_count=n,
        effective_population=effective_population,
        headline_percentile=config.headline_percentile,
        headline_projected_micro_usd=headline,
        baseline_mean_micro_usd=baseline_mean,
        projected_mean_micro_usd=projected_mean,
        percentiles=tuple(stats),
        blowup_risk=blowup_risk,
        drivers=drivers,
        residual_micro_usd=residual,
    )
