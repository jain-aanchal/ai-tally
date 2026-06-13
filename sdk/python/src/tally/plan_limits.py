# SPDX-License-Identifier: Apache-2.0
"""Plan tiers + graceful usage-limit enforcement + upgrade prompts (CTO-89).

Why this module exists
----------------------
ai-tally bills on **billable telemetry the customer is paying to send us**.
That creates a hard product constraint: a usage limit must *never* become a
reason to drop that telemetry. If a customer blows past their plan's
trace-count or active-feature-count for the period, the correct response is to
**accept the data and prompt them to upgrade** — not to silently lose the very
events they are paying for. Dropping billable data would both break their
observability and quietly undercharge us; both are unacceptable.

So enforcement here is a **graceful, escalating ladder**, not a gate:

    OK  →  WARN (>=80% of cap)  →  SOFT_CAP (>=100%, still accepting)  →
    UPGRADE_REQUIRED (well over, upgrade strongly indicated)

Every level above ``OK`` carries a human-readable upgrade prompt. The crucial
invariant is encoded as :attr:`EnforcementDecision.drops_data`, which is
**always False**: no decision this module can produce ever instructs a caller
to drop billable data.

Two meters are tracked per billing period:

* **head trace-count** — every trace counted at HEAD (before sampling), the
  canonical billable unit (mirrors :class:`tally.sampling.BillingMeter`).
* **distinct active feature-count** — the number of distinct feature tags seen
  in the period (plan tiers cap how many product features a customer may meter).

Built-in tiers FREE / PRO / SCALE / ENTERPRISE are provided (enterprise =
effectively unlimited, represented as ``None`` per limit), and custom tiers can
be defined. A :class:`PlanRegistry` protocol (with an in-memory default impl)
resolves a tenant's tier so callers can inject their own billing source.

Nothing here raises on boundary junk in the hot-path classify function:
negative usage is clamped to zero rather than crashing a metering callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

#: Fraction of a cap at or above which we start warning (but stay fully OK to send).
WARN_THRESHOLD = 0.8
#: Multiple of a cap at or above which we escalate SOFT_CAP -> UPGRADE_REQUIRED.
#: i.e. once usage is >=150% of the cap, an upgrade is strongly indicated.
UPGRADE_REQUIRED_MULTIPLE = 1.5


# --------------------------------------------------------------------------- #
# Meters
# --------------------------------------------------------------------------- #
class Meter(str, Enum):
    """The two metered dimensions a plan tier caps, per billing period."""

    TRACES = "traces"  # head trace-count (billable unit)
    FEATURES = "features"  # distinct active feature tags


# --------------------------------------------------------------------------- #
# Plan tiers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PlanTier:
    """A named plan tier with per-meter caps for a billing period.

    ``max_traces_per_period`` caps head trace-count; ``max_features`` caps the
    number of distinct active feature tags. ``None`` for either means
    **unlimited** for that meter (enterprise tiers use this). ``rank`` orders
    tiers for upgrade computation (cheaper/smaller first); ``price_micro_usd``
    is the monthly list price in micro-USD, used only to break ties when picking
    the cheapest qualifying upgrade.
    """

    name: str
    max_traces_per_period: int | None
    max_features: int | None
    rank: int = 0
    price_micro_usd: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("PlanTier.name must be non-empty")
        if self.max_traces_per_period is not None and self.max_traces_per_period < 0:
            raise ValueError(
                f"max_traces_per_period must be >= 0 or None, got {self.max_traces_per_period}"
            )
        if self.max_features is not None and self.max_features < 0:
            raise ValueError(f"max_features must be >= 0 or None, got {self.max_features}")
        if self.rank < 0:
            raise ValueError(f"rank must be >= 0, got {self.rank}")
        if self.price_micro_usd < 0:
            raise ValueError(f"price_micro_usd must be >= 0, got {self.price_micro_usd}")

    def limit_for(self, meter: Meter) -> int | None:
        """Return this tier's cap for *meter* (``None`` == unlimited)."""
        if meter is Meter.TRACES:
            return self.max_traces_per_period
        return self.max_features

    def is_unlimited(self, meter: Meter) -> bool:
        """True iff this tier places no cap on *meter*."""
        return self.limit_for(meter) is None

    def covers(self, usage: UsageSnapshot) -> bool:
        """True iff *usage* fits within this tier's caps on **both** meters."""
        for meter in Meter:
            limit = self.limit_for(meter)
            if limit is not None and usage.value_for(meter) > limit:
                return False
        return True


# Built-in tiers. Numbers are illustrative-but-sensible monthly caps; enterprise
# is effectively unlimited (None per meter).
FREE = PlanTier(
    name="FREE",
    max_traces_per_period=10_000,
    max_features=3,
    rank=0,
    price_micro_usd=0,
)
PRO = PlanTier(
    name="PRO",
    max_traces_per_period=1_000_000,
    max_features=25,
    rank=1,
    price_micro_usd=99_000_000,
)
SCALE = PlanTier(
    name="SCALE",
    max_traces_per_period=25_000_000,
    max_features=200,
    rank=2,
    price_micro_usd=499_000_000,
)
ENTERPRISE = PlanTier(
    name="ENTERPRISE",
    max_traces_per_period=None,  # unlimited
    max_features=None,  # unlimited
    rank=3,
    price_micro_usd=2_500_000_000,
)

#: Built-in tiers, ordered cheapest/smallest -> largest.
BUILTIN_TIERS: tuple[PlanTier, ...] = (FREE, PRO, SCALE, ENTERPRISE)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Abstract meter readings for a tenant in the current billing period.

    Deliberately plain ints so this module is independent of any metering
    submodule: a caller feeds in whatever head trace-count and distinct
    feature-count it has. Negative inputs are clamped to zero (boundary junk
    must not crash a metering callback).
    """

    traces: int
    features: int

    def __post_init__(self) -> None:
        # Clamp rather than raise: this rides the metering hot path.
        if self.traces < 0:
            object.__setattr__(self, "traces", 0)
        if self.features < 0:
            object.__setattr__(self, "features", 0)

    def value_for(self, meter: Meter) -> int:
        return self.traces if meter is Meter.TRACES else self.features


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #
class EnforcementLevel(str, Enum):
    """Escalating ladder. Crucially, **none** of these means "drop data"."""

    OK = "ok"  # comfortably under cap
    WARN = "warn"  # >=80% of cap — surface a heads-up
    SOFT_CAP = "soft_cap"  # >=100% of cap — still accepting, prompt to upgrade
    UPGRADE_REQUIRED = "upgrade_required"  # well over — upgrade strongly indicated

    @property
    def severity(self) -> int:
        """Numeric ordering so the worse of two levels can be selected."""
        return {
            EnforcementLevel.OK: 0,
            EnforcementLevel.WARN: 1,
            EnforcementLevel.SOFT_CAP: 2,
            EnforcementLevel.UPGRADE_REQUIRED: 3,
        }[self]


@dataclass(frozen=True, slots=True)
class EnforcementDecision:
    """Outcome of classifying one tenant's usage against one tier.

    Carries the level, which meter tripped (the worst one), the usage/limit
    numbers, percent-used and a human-readable upgrade prompt. ``limit`` is
    ``None`` when the tripped meter is unlimited (only possible at ``OK``).
    """

    level: EnforcementLevel
    tier_name: str
    meter: Meter
    usage: int
    limit: int | None
    percent_used: float
    message: str

    @property
    def drops_data(self) -> bool:
        """**Always False.** Billable telemetry is never dropped — this module
        only ever warns or prompts an upgrade. The invariant exists so callers
        can assert on it and never gate ingestion on a plan limit.
        """
        return False

    @property
    def needs_upgrade(self) -> bool:
        """True once the customer is at or over a cap (SOFT_CAP or beyond)."""
        return self.level.severity >= EnforcementLevel.SOFT_CAP.severity

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "tier_name": self.tier_name,
            "meter": self.meter.value,
            "usage": self.usage,
            "limit": self.limit,
            "percent_used": self.percent_used,
            "needs_upgrade": self.needs_upgrade,
            "drops_data": self.drops_data,
            "message": self.message,
        }

    def summary(self) -> str:
        cap = "unlimited" if self.limit is None else str(self.limit)
        return (
            f"[{self.level.value}] {self.tier_name} {self.meter.value}: "
            f"{self.usage}/{cap} ({self.percent_used:.0f}%)"
        )


def _percent(usage: int, limit: int | None) -> float:
    """Percent of cap used. Unlimited -> 0.0; zero cap with usage -> inf."""
    if limit is None:
        return 0.0
    if limit == 0:
        return float("inf") if usage > 0 else 0.0
    return (usage / limit) * 100.0


def _classify_meter(tier: PlanTier, usage: UsageSnapshot, meter: Meter) -> EnforcementDecision:
    """Classify a single meter against the tier's cap for it."""
    value = usage.value_for(meter)
    limit = tier.limit_for(meter)
    pct = _percent(value, limit)

    if limit is None:
        level = EnforcementLevel.OK
    elif limit == 0:
        # A zero cap means the feature is disabled on this tier: any usage is
        # immediately over. Still never drops data — prompt to upgrade.
        level = EnforcementLevel.UPGRADE_REQUIRED if value > 0 else EnforcementLevel.OK
    elif value >= limit * UPGRADE_REQUIRED_MULTIPLE:
        level = EnforcementLevel.UPGRADE_REQUIRED
    elif value >= limit:
        level = EnforcementLevel.SOFT_CAP
    elif value >= limit * WARN_THRESHOLD:
        level = EnforcementLevel.WARN
    else:
        level = EnforcementLevel.OK

    return EnforcementDecision(
        level=level,
        tier_name=tier.name,
        meter=meter,
        usage=value,
        limit=limit,
        percent_used=pct,
        message=_message_for(level, tier, meter, value, limit, pct),
    )


def _message_for(
    level: EnforcementLevel,
    tier: PlanTier,
    meter: Meter,
    usage: int,
    limit: int | None,
    pct: float,
) -> str:
    """Human-readable prompt for a level. Always upgrade-oriented, never punitive."""
    label = "traces" if meter is Meter.TRACES else "active features"
    if level is EnforcementLevel.OK:
        return f"{tier.name}: {usage} {label} used — within plan limits."
    if level is EnforcementLevel.WARN:
        return (
            f"{tier.name}: {usage} of {limit} {label} used ({pct:.0f}%). "
            f"Approaching your plan limit — consider upgrading to avoid surprises."
        )
    if level is EnforcementLevel.SOFT_CAP:
        return (
            f"{tier.name}: {usage} {label} used, over your plan limit of {limit}. "
            f"We're still accepting all your data — upgrade to raise this limit."
        )
    # UPGRADE_REQUIRED
    return (
        f"{tier.name}: {usage} {label} used, well over your plan limit of {limit}. "
        f"Your data is still being collected — please upgrade your plan."
    )


def classify(tier: PlanTier, usage: UsageSnapshot) -> EnforcementDecision:
    """Classify *usage* against *tier*, returning the **worst** meter's decision.

    Both meters are evaluated; the decision for the meter with the higher
    severity is returned (ties break to TRACES, the billable unit). Never raises
    — :class:`UsageSnapshot` has already clamped boundary junk.
    """
    decisions = [_classify_meter(tier, usage, meter) for meter in Meter]
    # Higher severity wins; on a tie keep the first (TRACES, evaluated first).
    return max(decisions, key=lambda d: d.level.severity)


# --------------------------------------------------------------------------- #
# Limit-state visibility
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MeterState:
    """Per-meter usage-vs-cap line for a UI/customer summary."""

    meter: Meter
    usage: int
    limit: int | None
    percent_used: float
    level: EnforcementLevel

    def as_dict(self) -> dict[str, object]:
        return {
            "meter": self.meter.value,
            "usage": self.usage,
            "limit": self.limit,
            "percent_used": self.percent_used,
            "level": self.level.value,
        }


@dataclass(frozen=True, slots=True)
class LimitState:
    """Full limit-state summary for a tenant: overall level + per-meter lines.

    This is what a "usage vs cap" UI renders. ``overall`` is the worst meter's
    decision (same as :func:`classify`); ``meters`` carries every meter so a
    dashboard can show both bars.
    """

    tier_name: str
    overall: EnforcementDecision
    meters: tuple[MeterState, ...]

    @property
    def needs_upgrade(self) -> bool:
        return self.overall.needs_upgrade

    @property
    def drops_data(self) -> bool:
        """Always False — surfaced here too so the UI can state it plainly."""
        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "tier_name": self.tier_name,
            "overall": self.overall.as_dict(),
            "meters": [m.as_dict() for m in self.meters],
            "needs_upgrade": self.needs_upgrade,
            "drops_data": self.drops_data,
        }

    def summary(self) -> str:
        lines = "; ".join(
            f"{m.meter.value} {m.usage}/"
            f"{'unlimited' if m.limit is None else m.limit} ({m.percent_used:.0f}%)"
            for m in self.meters
        )
        return f"{self.tier_name} [{self.overall.level.value}]: {lines}"


def build_limit_state(tier: PlanTier, usage: UsageSnapshot) -> LimitState:
    """Build a full :class:`LimitState` (overall + per-meter) for a UI."""
    per_meter = [_classify_meter(tier, usage, meter) for meter in Meter]
    overall = max(per_meter, key=lambda d: d.level.severity)
    meters = tuple(
        MeterState(
            meter=d.meter,
            usage=d.usage,
            limit=d.limit,
            percent_used=d.percent_used,
            level=d.level,
        )
        for d in per_meter
    )
    return LimitState(tier_name=tier.name, overall=overall, meters=meters)


# --------------------------------------------------------------------------- #
# Upgrade flow
# --------------------------------------------------------------------------- #
def next_tier_up(
    current: PlanTier,
    usage: UsageSnapshot,
    *,
    tiers: tuple[PlanTier, ...] = BUILTIN_TIERS,
) -> PlanTier | None:
    """Cheapest tier strictly above *current* whose caps clear *usage*.

    "Cheapest" = lowest ``(rank, price_micro_usd)`` among qualifying tiers ranked
    above ``current``. Returns ``None`` when the customer is already on the top
    tier (or no higher tier clears their usage). Pure: no side effects.
    """
    candidates = [
        t
        for t in tiers
        if t.rank > current.rank and t.covers(usage)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda t: (t.rank, t.price_micro_usd))


def apply_tier_change(new_tier: PlanTier, usage: UsageSnapshot) -> EnforcementDecision:
    """Re-classify *usage* under *new_tier* so the change "takes effect promptly".

    Pure function: returns the decision a caller would see immediately after
    switching plans, without mutating anything.
    """
    return classify(new_tier, usage)


# --------------------------------------------------------------------------- #
# Tier registry
# --------------------------------------------------------------------------- #
@runtime_checkable
class PlanRegistry(Protocol):
    """Resolves a tenant's current plan tier (billing-source backed in prod)."""

    def tier_for(self, tenant_id: str) -> PlanTier: ...


class InMemoryPlanRegistry:
    """Per-tenant tier registry with a configurable default (defaults to FREE)."""

    __slots__ = ("_by_tenant", "_default")

    def __init__(self, default: PlanTier | None = None) -> None:
        self._default = default if default is not None else FREE
        self._by_tenant: dict[str, PlanTier] = {}

    def set_tier(self, tenant_id: str, tier: PlanTier) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        self._by_tenant[tenant_id] = tier

    def tier_for(self, tenant_id: str) -> PlanTier:
        return self._by_tenant.get(tenant_id, self._default)
