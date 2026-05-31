"""Tail-weighted sampling for pre-deploy estimation (CTO-71, spec §9 W3).

Pre-deploy estimation answers "what will switching model/prompt cost and how will quality move?" by
*replaying* a representative sample of a workload's historical prompts. The trap: agent cost and
failure are a power law — the blow-ups (retry loops, step-cap hits, long-context premium calls) live
in the **tail**. A sample drawn from *average* prompts would systematically under-predict both cost
and risk, because the expensive tail is exactly what it misses.

So this sampler deliberately **over-indexes the expensive end**:

* It defines the "tail" as runs at/above a configurable cost percentile (P90 by default) and draws
  the bulk of the budget from there.
* It **mandatorily includes** the top-K most expensive runs and any run that hit a retry loop or a
  step cap — the pathological cases an estimate must never skip.
* The rest of the budget is a random body sample, so the composition still reflects ordinary
  traffic.

It is **deterministic and reproducible**: selection within each pool is ranked by a seeded hash of
``run_id``, so the same workload + seed always yields the same sample — reruns and CI are stable.
Pure logic; no infra. The projection math that consumes this sample is CTO-72 (out of scope here).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

__all__ = [
    "HistoricalRun",
    "TailSampleConfig",
    "SampleComposition",
    "TailSample",
    "tail_weighted_sample",
]


@dataclass(frozen=True, slots=True)
class HistoricalRun:
    """One historical run in a workload, with the signals that drive tail weighting.

    ``cost_micro_usd`` is the expense signal (integer micro-USD, consistent with
    :mod:`tally.pricing` — never float). ``hit_retry_loop`` / ``hit_step_cap`` flag
    pathological runs that must always be included in an estimation sample regardless of cost.
    """

    run_id: str
    cost_micro_usd: int = 0
    hit_retry_loop: bool = False
    hit_step_cap: bool = False

    @property
    def is_pathological(self) -> bool:
        return self.hit_retry_loop or self.hit_step_cap


@dataclass(frozen=True, slots=True)
class TailSampleConfig:
    """Sampling budget + tail-weighting knobs.

    ``sample_size`` is the target number of runs to select. Of the budget left after mandatory
    inclusions, ``tail_fraction`` is drawn from the tail (cost >= the ``tail_percentile``
    cutoff) and the remainder is a random body sample. ``top_k_expensive`` runs are always included.
    """

    sample_size: int = 180
    tail_fraction: float = 0.7
    tail_percentile: float = 90.0
    top_k_expensive: int = 10
    seed: int = 0

    def __post_init__(self) -> None:
        if not (0.0 <= self.tail_fraction <= 1.0):
            raise ValueError(f"tail_fraction must be in [0, 1], got {self.tail_fraction}")
        if not (0.0 <= self.tail_percentile <= 100.0):
            raise ValueError(f"tail_percentile must be in [0, 100], got {self.tail_percentile}")


@dataclass(frozen=True, slots=True)
class SampleComposition:
    """Human-readable breakdown of how a sample was assembled (reported to the user)."""

    total: int
    mandatory_top_k: int
    mandatory_retry_or_cap: int
    tail_weighted: int
    random: int
    tail_percentile: float
    tail_cost_cutoff: int

    @property
    def mandatory(self) -> int:
        return self.mandatory_top_k + self.mandatory_retry_or_cap

    def summary(self) -> str:
        """e.g. ``"140 tail-weighted + 40 random (+12 mandatory: 10 top-cost, 2 cap)"``."""
        return (
            f"{self.tail_weighted} tail-weighted + {self.random} random "
            f"(+{self.mandatory} mandatory: {self.mandatory_top_k} top-cost, "
            f"{self.mandatory_retry_or_cap} retry/step-cap)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "mandatory_top_k": self.mandatory_top_k,
            "mandatory_retry_or_cap": self.mandatory_retry_or_cap,
            "mandatory": self.mandatory,
            "tail_weighted": self.tail_weighted,
            "random": self.random,
            "tail_percentile": self.tail_percentile,
            "tail_cost_cutoff": self.tail_cost_cutoff,
            "summary": self.summary(),
        }


@dataclass(frozen=True, slots=True)
class TailSample:
    """The selected sample plus its composition report."""

    runs: tuple[HistoricalRun, ...]
    composition: SampleComposition

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(r.run_id for r in self.runs)


def _seeded_rank(seed: int, run_id: str) -> float:
    """Deterministic [0, 1) rank for a run under a seed — stable across runs, varies with seed."""
    digest = hashlib.sha256(f"{seed}:{run_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _percentile_cutoff(sorted_costs: list[int], percentile: float) -> int:
    """Nearest-rank percentile cutoff over ascending ``sorted_costs``. Empty → 0.

    Returns the smallest cost value at/above which a run is considered "tail". Deterministic; no
    interpolation so the cutoff is always an actual observed cost.
    """
    if not sorted_costs:
        return 0
    if percentile <= 0:
        return sorted_costs[0]
    if percentile >= 100:
        return sorted_costs[-1]
    # nearest-rank: rank = ceil(p/100 * N), 1-based
    rank = -(-int(percentile) * len(sorted_costs) // 100)  # ceil division
    rank = min(max(rank, 1), len(sorted_costs))
    return sorted_costs[rank - 1]


def _pick(pool: list[HistoricalRun], count: int, seed: int) -> list[HistoricalRun]:
    """Deterministically take ``count`` runs from ``pool`` ranked by seeded hash (lowest first)."""
    if count <= 0 or not pool:
        return []
    ranked = sorted(pool, key=lambda r: (_seeded_rank(seed, r.run_id), r.run_id))
    return ranked[:count]


def tail_weighted_sample(
    runs: list[HistoricalRun] | tuple[HistoricalRun, ...],
    config: TailSampleConfig | None = None,
) -> TailSample:
    """Select a tail-weighted, reproducible estimation sample from a workload's historical runs.

    Order of selection:

    1. **Mandatory** — the ``top_k_expensive`` most expensive runs, plus every run that hit a retry
       loop or step cap. These are always included (capped at ``sample_size``).
    2. **Tail-weighted** — ``tail_fraction`` of the remaining budget, drawn from runs at/above the
       ``tail_percentile`` cost cutoff.
    3. **Random body** — the rest of the budget, drawn from the cheaper body.

    Pools 2 and 3 are sampled by seeded hash for determinism. Shortfalls cascade (a thin tail pool
    spills its budget to the body and vice-versa) so the sample reaches ``sample_size`` whenever the
    workload is large enough. Never raises on empty/degenerate input.
    """
    config = config or TailSampleConfig()
    pool = [r for r in runs if isinstance(r, HistoricalRun)]
    n = len(pool)
    cutoff = _percentile_cutoff(sorted(r.cost_micro_usd for r in pool), config.tail_percentile)

    # Whole workload fits in the budget → take everything (still report composition honestly).
    target = min(config.sample_size, n)

    # --- 1. mandatory: top-K most expensive ∪ retry/step-cap runs --------------------------------
    by_cost_desc = sorted(pool, key=lambda r: (-r.cost_micro_usd, r.run_id))
    top_k_ids = {r.run_id for r in by_cost_desc[: max(0, config.top_k_expensive)]}
    mandatory: list[HistoricalRun] = []
    mandatory_ids: set[str] = set()
    for r in pool:
        if r.run_id in top_k_ids or r.is_pathological:
            mandatory.append(r)
            mandatory_ids.add(r.run_id)
    # If mandatory alone overflows the budget, keep the most expensive of them.
    if len(mandatory) > target:
        mandatory = sorted(mandatory, key=lambda r: (-r.cost_micro_usd, r.run_id))[:target]
        mandatory_ids = {r.run_id for r in mandatory}

    n_top_k = sum(1 for r in mandatory if r.run_id in top_k_ids)
    n_retry_cap = len(mandatory) - n_top_k

    # --- 2/3. fill the rest from tail then body --------------------------------------------------
    remaining = target - len(mandatory)
    tail_pool = [r for r in pool if r.run_id not in mandatory_ids and r.cost_micro_usd >= cutoff]
    body_pool = [r for r in pool if r.run_id not in mandatory_ids and r.cost_micro_usd < cutoff]

    want_tail = min(round(remaining * config.tail_fraction), remaining)
    tail_pick = _pick(tail_pool, want_tail, config.seed)
    # body gets the remainder, including any tail shortfall.
    want_body = remaining - len(tail_pick)
    body_pick = _pick(body_pool, want_body, config.seed)
    # if the body pool was also thin, spill the remaining budget back to the tail pool.
    if len(body_pick) < want_body:
        leftover = want_body - len(body_pick)
        already = {r.run_id for r in tail_pick}
        spill_pool = [r for r in tail_pool if r.run_id not in already]
        tail_pick = tail_pick + _pick(spill_pool, leftover, config.seed)

    selected = mandatory + tail_pick + body_pick
    composition = SampleComposition(
        total=len(selected),
        mandatory_top_k=n_top_k,
        mandatory_retry_or_cap=n_retry_cap,
        tail_weighted=len(tail_pick),
        random=len(body_pick),
        tail_percentile=config.tail_percentile,
        tail_cost_cutoff=cutoff,
    )
    return TailSample(runs=tuple(selected), composition=composition)
