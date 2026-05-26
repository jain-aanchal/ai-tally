"""Deterministic, stratified, whole-trace sampling — with billing decoupled from sampling.

Implements CTO-50.

Why stratified: agent cost is a power law. Uniform sampling would lose the expensive tail (the
part that matters) and add huge variance to extrapolated cost. So we sample the cheap, high-volume
body down and keep the expensive tail at ~100%. Extrapolated analytics cost is then computed
per-stratum as ``sum(cost_i / sample_rate_i)`` — the tail is exact, error is confined to the cheap
body where it is harmless.

Why deterministic + whole-trace: the keep/drop decision is a pure function of ``trace_id`` (+ the
configured rate), so every span in a trace shares one decision and the same trace always decides
the same way. Never partial.

Billing is **decoupled**: :class:`BillingMeter` counts every trace at HEAD, *before* the sampling
decision, so invoices are exact regardless of the analytics sample rate (per CTO-21 / CTO-84).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class Stratum(str, Enum):
    """Cost/complexity stratum chosen at head time, cheapest → most expensive."""

    BODY = "body"  # cheap, high-volume single-shot calls
    MID = "mid"
    TAIL = "tail"  # expensive: agents, long context, top-tier models


@dataclass(frozen=True, slots=True)
class TraceSignals:
    """Head-time signals available before the call completes."""

    is_agent: bool = False
    prompt_tokens_estimate: int = 0
    model_tier: str = "standard"  # "standard" | "premium"


@dataclass(frozen=True, slots=True)
class SamplingConfig:
    """Per-stratum keep rates in [0, 1]. Tail defaults to 1.0 (always keep)."""

    body_rate: float = 0.1
    mid_rate: float = 0.5
    tail_rate: float = 1.0
    #: per-feature-tag overrides of the *whole* config's effective rate (applied to all strata)
    feature_overrides: dict[str, float] = field(default_factory=dict)
    #: thresholds for stratum assignment
    mid_prompt_tokens: int = 2_000
    tail_prompt_tokens: int = 8_000

    def __post_init__(self) -> None:
        for name in ("body_rate", "mid_rate", "tail_rate"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0, 1], got {v}")


@dataclass(frozen=True, slots=True)
class SampleDecision:
    keep: bool
    sample_rate: float
    stratum: Stratum


def assign_stratum(signals: TraceSignals, config: SamplingConfig) -> Stratum:
    """Deterministically map head-time signals to a stratum (tail = expensive)."""
    if (
        signals.is_agent
        or signals.model_tier == "premium"
        or signals.prompt_tokens_estimate >= config.tail_prompt_tokens
    ):
        return Stratum.TAIL
    if signals.prompt_tokens_estimate >= config.mid_prompt_tokens:
        return Stratum.MID
    return Stratum.BODY


def _unit_hash(trace_id: str) -> float:
    """Map a trace_id deterministically to a float in [0, 1)."""
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    # 8 bytes → 64-bit int → [0,1)
    n = int.from_bytes(digest[:8], "big")
    return n / 2**64


class Sampler:
    """Deterministic stratified sampler. Pure: same (trace_id, signals) → same decision."""

    def __init__(self, config: SamplingConfig | None = None) -> None:
        self.config = config or SamplingConfig()

    def _rate_for(self, stratum: Stratum, feature_tag: str | None) -> float:
        if feature_tag and feature_tag in self.config.feature_overrides:
            return self.config.feature_overrides[feature_tag]
        return {
            Stratum.BODY: self.config.body_rate,
            Stratum.MID: self.config.mid_rate,
            Stratum.TAIL: self.config.tail_rate,
        }[stratum]

    def decide(
        self,
        trace_id: str,
        signals: TraceSignals | None = None,
        *,
        feature_tag: str | None = None,
    ) -> SampleDecision:
        stratum = assign_stratum(signals or TraceSignals(), self.config)
        rate = self._rate_for(stratum, feature_tag)
        keep = rate >= 1.0 or _unit_hash(trace_id) < rate
        return SampleDecision(keep=keep, sample_rate=rate, stratum=stratum)


@dataclass(slots=True)
class BillingMeter:
    """Head-count meter — counts every trace BEFORE the sampling decision.

    Tamper-evidence and reconciliation against ingest live server-side (CTO-84); this is the SDK
    hook that guarantees a billable trace is counted exactly once regardless of sampling.
    """

    trace_count: int = 0
    _seen: set[str] = field(default_factory=set)

    def count_trace(self, trace_id: str) -> None:
        if trace_id not in self._seen:
            self._seen.add(trace_id)
            self.trace_count += 1


def extrapolate_cost(samples: list[tuple[int, float]]) -> int:
    """Extrapolate total cost (micro-USD) from kept samples.

    Args:
        samples: list of ``(cost_micro_usd, sample_rate)`` for kept traces.

    Returns:
        Estimated total cost = ``sum(cost_i / rate_i)``. Per-stratum rates mean the tail (rate 1.0)
        contributes exactly and only the cheap body is scaled up.
    """
    total = 0.0
    for cost, rate in samples:
        if rate <= 0:
            continue
        total += cost / rate
    return round(total)
