"""Reconciler — cloud-billing true-up (CTO-64).

We bill and display two cost tracks and keep them honest by never conflating them:

- **estimated** cost is computed live at ingest from the price catalog (cheap, instant, but a
  model — it can drift from what the cloud actually charges);
- **reconciled** cost is the truth from the provider's invoice, which lags 24–48h.

This module trues the first up against the second. A daily job feeds it (a) the estimated
per-feature cost for a day and (b) the actual cloud-billing line items for that day; it maps each
billing line's resource tag to a feature tag, allocates genuinely shared infrastructure across
features by query volume, and emits a **reconciled** cost per feature plus a human-readable delta
event ("est \\$X → reconciled \\$Y, +Z%").

Two honesty rules drive the design:

1. **Respect the billing lag.** A day whose invoice has not settled yet (less than ``lag_hours``
   since the day closed) is *not* trued up — its rows stay ``CostSource.ESTIMATED`` and the day is
   reported as skipped, so we never publish a half-arrived invoice as final.
2. **Shared cost is allocated, not double-counted.** Billing lines that map to no single feature
   (shared DB, NAT gateway, …) go into a pool that is split across the day's features in
   proportion to their query count, with integer micro-USD and a largest-remainder split so the
   parts sum back to the pool exactly.

Money is integer micro-USD throughout (see :mod:`tally.schema`); proportional math uses
:class:`~decimal.Decimal`. Pure functions over plain dataclasses — no infra, no network (the
caller fetches billing; cf. the connector framework CTO-63). Deliberately self-contained: it does
not import the connector module, so it consumes abstract cost rows rather than a specific source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from tally.schema import DEFAULT_CURRENCY, micro_to_usd

#: Feature tag used when shared cost cannot be attributed to any real feature.
UNATTRIBUTED = "unattributed"


class CostSource(str, Enum):
    """Which track a cost figure comes from. Written alongside the amount so the UI and billing
    never mistake a still-estimated number for a settled one."""

    ESTIMATED = "estimated"
    RECONCILED = "reconciled"


# --- inputs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EstimatedCostRow:
    """The estimated cost for one feature on one day, plus the query count used to allocate shared
    infrastructure cost. ``query_count`` is the number of traces/queries the feature drove that
    day — the weight by which a shared bill is split."""

    feature_tag: str
    day: date
    estimated_micro_usd: int
    query_count: int = 0

    def __post_init__(self) -> None:
        if not self.feature_tag:
            raise ValueError("feature_tag must be non-empty")
        if not isinstance(self.day, date):
            raise ValueError("day must be a datetime.date")
        if isinstance(self.estimated_micro_usd, bool) or not isinstance(
            self.estimated_micro_usd, int
        ):
            raise ValueError("estimated_micro_usd must be an int (micro-USD)")
        if self.estimated_micro_usd < 0:
            raise ValueError("estimated_micro_usd must be >= 0")
        if isinstance(self.query_count, bool) or not isinstance(self.query_count, int):
            raise ValueError("query_count must be an int")
        if self.query_count < 0:
            raise ValueError("query_count must be >= 0")


@dataclass(frozen=True, slots=True)
class CloudBillingLineItem:
    """One actual cloud-billing line for a day: a resource tag and its real cost. ``resource_tag``
    is mapped to a feature tag via the reconciler's ``tag_map``; a line that maps to nothing is
    treated as shared and allocated by query count."""

    resource_tag: str
    day: date
    cost_micro_usd: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise ValueError("day must be a datetime.date")
        if isinstance(self.cost_micro_usd, bool) or not isinstance(self.cost_micro_usd, int):
            raise ValueError("cost_micro_usd must be an int (micro-USD)")
        if self.cost_micro_usd < 0:
            raise ValueError("cost_micro_usd must be >= 0")


@dataclass(frozen=True, slots=True)
class ReconcilerConfig:
    """Tuning. ``lag_hours`` is how long after a day closes we wait before trusting its invoice as
    final (default 48h, the upper end of the typical cloud-billing lag)."""

    lag_hours: int = 48

    def __post_init__(self) -> None:
        if self.lag_hours < 0:
            raise ValueError("lag_hours must be >= 0")


# --- outputs ------------------------------------------------------------------------------------


def _pct_change(base: int, new: int) -> float | None:
    """Percent change from ``base`` to ``new``; None when ``base`` is zero (undefined)."""
    if base == 0:
        return None
    return (new - base) / base * 100.0


@dataclass(frozen=True, slots=True)
class ReconciledCostRow:
    """Reconciled cost for one feature on one day. ``cost_source`` is RECONCILED once the day has
    settled, otherwise ESTIMATED (the true-up has not happened yet)."""

    feature_tag: str
    day: date
    estimated_micro_usd: int
    reconciled_micro_usd: int
    cost_source: CostSource
    settled: bool

    @property
    def delta_micro_usd(self) -> int:
        return self.reconciled_micro_usd - self.estimated_micro_usd

    @property
    def delta_pct(self) -> float | None:
        return _pct_change(self.estimated_micro_usd, self.reconciled_micro_usd)

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_tag": self.feature_tag,
            "day": self.day.isoformat(),
            "estimated_micro_usd": self.estimated_micro_usd,
            "reconciled_micro_usd": self.reconciled_micro_usd,
            "cost_source": self.cost_source.value,
            "settled": self.settled,
            "delta_micro_usd": self.delta_micro_usd,
            "delta_pct": self.delta_pct,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationDelta:
    """A settled change worth surfacing: "est \\$X → reconciled \\$Y, +Z%"."""

    feature_tag: str
    day: date
    estimated_micro_usd: int
    reconciled_micro_usd: int

    @property
    def delta_micro_usd(self) -> int:
        return self.reconciled_micro_usd - self.estimated_micro_usd

    @property
    def delta_pct(self) -> float | None:
        return _pct_change(self.estimated_micro_usd, self.reconciled_micro_usd)

    def summary(self) -> str:
        est = micro_to_usd(self.estimated_micro_usd)
        rec = micro_to_usd(self.reconciled_micro_usd)
        pct = self.delta_pct
        if pct is None:
            change = "new" if self.reconciled_micro_usd else "0%"
        else:
            change = f"{pct:+.1f}%"
        return (
            f"{self.feature_tag} on {self.day.isoformat()}: "
            f"est ${est} → reconciled ${rec}, {change}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_tag": self.feature_tag,
            "day": self.day.isoformat(),
            "estimated_micro_usd": self.estimated_micro_usd,
            "reconciled_micro_usd": self.reconciled_micro_usd,
            "delta_micro_usd": self.delta_micro_usd,
            "delta_pct": self.delta_pct,
            "summary": self.summary(),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Everything the daily job produced: per-(feature, day) reconciled rows, the notable delta
    events, and the days held back because their invoice has not settled."""

    rows: tuple[ReconciledCostRow, ...] = ()
    deltas: tuple[ReconciliationDelta, ...] = ()
    skipped_unsettled_days: tuple[date, ...] = ()

    @property
    def total_estimated_micro_usd(self) -> int:
        return sum(r.estimated_micro_usd for r in self.rows)

    @property
    def total_reconciled_micro_usd(self) -> int:
        return sum(r.reconciled_micro_usd for r in self.rows)

    def summary(self) -> str:
        est = micro_to_usd(self.total_estimated_micro_usd)
        rec = micro_to_usd(self.total_reconciled_micro_usd)
        settled = sum(1 for r in self.rows if r.settled)
        return (
            f"{len(self.rows)} rows ({settled} reconciled), "
            f"est ${est} → reconciled ${rec}, "
            f"{len(self.deltas)} deltas, {len(self.skipped_unsettled_days)} day(s) pending"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": [r.as_dict() for r in self.rows],
            "deltas": [d.as_dict() for d in self.deltas],
            "skipped_unsettled_days": [d.isoformat() for d in self.skipped_unsettled_days],
            "total_estimated_micro_usd": self.total_estimated_micro_usd,
            "total_reconciled_micro_usd": self.total_reconciled_micro_usd,
            "summary": self.summary(),
        }


# --- helpers ------------------------------------------------------------------------------------


def _is_settled(day: date, as_of: datetime, lag_hours: int) -> bool:
    """A day's invoice is final once ``lag_hours`` have passed since the day *closed* (midnight
    UTC after ``day``). Before that, the true-up is premature."""
    as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    day_close = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return as_of_utc >= day_close + timedelta(hours=lag_hours)


def _allocate_by_weight(total_micro: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split ``total_micro`` across keys in proportion to integer ``weights``, summing back to
    ``total_micro`` exactly (largest-remainder method). With no positive weight, split evenly.

    Used to spread a shared bill across the features that drove it, by query count.
    """
    keys = list(weights)
    if not keys or total_micro == 0:
        return {k: 0 for k in keys}

    total_weight = sum(max(0, weights[k]) for k in keys)
    if total_weight == 0:
        # no usage signal — divide as evenly as possible, remainder to the first keys
        base, rem = divmod(total_micro, len(keys))
        return {k: base + (1 if i < rem else 0) for i, k in enumerate(keys)}

    exact = {k: Decimal(total_micro) * Decimal(max(0, weights[k])) / Decimal(total_weight)
             for k in keys}
    floors = {k: int(exact[k]) for k in keys}
    remainder = total_micro - sum(floors.values())
    # hand out the leftover micro-USD to the largest fractional parts first
    order = sorted(keys, key=lambda k: exact[k] - floors[k], reverse=True)
    for k in order[:remainder]:
        floors[k] += 1
    return floors


def _coerce_estimated(rows: Iterable[object]) -> list[EstimatedCostRow]:
    return [r for r in rows if isinstance(r, EstimatedCostRow)]


def _coerce_billing(items: Iterable[object]) -> list[CloudBillingLineItem]:
    return [b for b in items if isinstance(b, CloudBillingLineItem)]


# --- the reconciler -----------------------------------------------------------------------------


def reconcile(
    estimated: Iterable[object],
    billing: Iterable[object],
    *,
    tag_map: Mapping[str, str] | None = None,
    as_of: datetime,
    config: ReconcilerConfig | None = None,
) -> ReconciliationReport:
    """True estimated cost up against actual cloud billing, day by day.

    ``tag_map`` maps a billing line's ``resource_tag`` to a feature tag; any line whose tag is not
    in the map is treated as shared and allocated across the day's features by query count.
    ``as_of`` is the wall-clock time the job runs (used with ``lag_hours`` to decide settlement).

    Never raises on malformed input — non-dataclass entries are skipped. Returns a
    :class:`ReconciliationReport`.
    """
    cfg = config or ReconcilerConfig()
    mapping = dict(tag_map or {})
    est_rows = _coerce_estimated(estimated)
    bill_rows = _coerce_billing(billing)

    # index by day
    days = sorted({r.day for r in est_rows} | {b.day for b in bill_rows})
    est_by_day: dict[date, dict[str, EstimatedCostRow]] = {}
    for r in est_rows:
        # if a (feature, day) repeats, fold the amounts/weights together
        bucket = est_by_day.setdefault(r.day, {})
        prior = bucket.get(r.feature_tag)
        if prior is None:
            bucket[r.feature_tag] = r
        else:
            bucket[r.feature_tag] = EstimatedCostRow(
                feature_tag=r.feature_tag,
                day=r.day,
                estimated_micro_usd=prior.estimated_micro_usd + r.estimated_micro_usd,
                query_count=prior.query_count + r.query_count,
            )

    bill_by_day: dict[date, list[CloudBillingLineItem]] = {}
    for b in bill_rows:
        bill_by_day.setdefault(b.day, []).append(b)

    out_rows: list[ReconciledCostRow] = []
    deltas: list[ReconciliationDelta] = []
    skipped: list[date] = []

    for day in days:
        est_features = est_by_day.get(day, {})
        settled = _is_settled(day, as_of, cfg.lag_hours)

        if not settled:
            # leave the day on the estimated track; nothing to true up yet
            skipped.append(day)
            for feature, row in est_features.items():
                out_rows.append(
                    ReconciledCostRow(
                        feature_tag=feature,
                        day=day,
                        estimated_micro_usd=row.estimated_micro_usd,
                        reconciled_micro_usd=row.estimated_micro_usd,
                        cost_source=CostSource.ESTIMATED,
                        settled=False,
                    )
                )
            continue

        # settled: split billing into directly-attributed vs shared pool
        direct: dict[str, int] = {}
        shared_pool = 0
        for line in bill_by_day.get(day, ()):
            feature = mapping.get(line.resource_tag)
            if feature:
                direct[feature] = direct.get(feature, 0) + line.cost_micro_usd
            else:
                shared_pool += line.cost_micro_usd

        # features in play this day = those with an estimate or any direct billing
        feature_set = set(est_features) | set(direct)
        if not feature_set and shared_pool:
            feature_set = {UNATTRIBUTED}

        weights = {f: (est_features[f].query_count if f in est_features else 0)
                   for f in feature_set}
        allocation = _allocate_by_weight(shared_pool, weights)

        for feature in sorted(feature_set):
            estimated_micro = (
                est_features[feature].estimated_micro_usd if feature in est_features else 0
            )
            reconciled_micro = direct.get(feature, 0) + allocation.get(feature, 0)
            out_rows.append(
                ReconciledCostRow(
                    feature_tag=feature,
                    day=day,
                    estimated_micro_usd=estimated_micro,
                    reconciled_micro_usd=reconciled_micro,
                    cost_source=CostSource.RECONCILED,
                    settled=True,
                )
            )
            if reconciled_micro != estimated_micro:
                deltas.append(
                    ReconciliationDelta(
                        feature_tag=feature,
                        day=day,
                        estimated_micro_usd=estimated_micro,
                        reconciled_micro_usd=reconciled_micro,
                    )
                )

    return ReconciliationReport(
        rows=tuple(out_rows),
        deltas=tuple(deltas),
        skipped_unsettled_days=tuple(skipped),
    )
