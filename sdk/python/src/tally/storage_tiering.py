# SPDX-License-Identifier: Apache-2.0
"""Storage tiering & TTL policy (CTO-29 / spec §5.1, Appendix A).

Keeping every raw span on hot SSD forever is the dominant storage cost. We tier by age:

* **hot** SSD — recent spans, fully queried at row granularity.
* **warm** volume — still raw, cheaper disk; powers the last-month deep dives.
* **cold** volume — still raw but object-store-backed; rare late-billing true-ups.
* after the cold horizon the **raw span is dropped** and only the daily rollup aggregate
  (``daily_feature_rollup``, CTO-24) survives — enough for YoY cohorts + reconciliation.

This module is the single source of truth for the tier boundaries. It both *classifies* a span's
tier at query time and *generates* the ClickHouse ``TTL`` DDL, so the table definition and the
runtime logic can never silently drift. Enterprise tenants get longer retention via a per-tenant
override; ClickHouse TTL is table-level, so the override is compiled into a ``multiIf`` expression
keyed on ``TenantId``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

UTC = timezone.utc

# Canonical default boundaries (days). hot < warm < cold; raw dropped at cold.
DEFAULT_HOT_DAYS = 7
DEFAULT_WARM_DAYS = 30
DEFAULT_COLD_DAYS = 90

# The dimensions the surviving aggregate is keyed by, post raw-drop (spec §5.1). Order is the
# ClickHouse rollup ORDER BY prefix; ``Day`` is the time bucket.
WARM_AGGREGATE_DIMENSIONS = ("TenantId", "FeatureTag", "Day", "GenAiResponseModel")


class StorageTier(str, Enum):
    """Where a span physically lives as a function of its age."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    # Raw row has been dropped by TTL; only the rollup aggregate remains queryable.
    AGGREGATE = "aggregate"


class TtlActionKind(str, Enum):
    MOVE = "move"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class TtlAction:
    """One clause of a ClickHouse ``TTL``: move to a volume, or delete, at ``age_days``."""

    age_days: int
    kind: TtlActionKind
    target: str | None = None  # volume name for MOVE; None for DELETE

    def to_sql(self, *, timestamp_column: str = "Timestamp") -> str:
        base = f"toDateTime({timestamp_column}) + INTERVAL {self.age_days} DAY"
        if self.kind is TtlActionKind.MOVE:
            return f"{base} TO VOLUME '{self.target}'"
        return f"{base} DELETE"


def _check_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class TieringPolicy:
    """Age boundaries + volume names driving both classification and TTL DDL generation."""

    hot_days: int = DEFAULT_HOT_DAYS
    warm_days: int = DEFAULT_WARM_DAYS
    cold_days: int = DEFAULT_COLD_DAYS
    warm_volume: str = "warm"
    cold_volume: str = "cold"

    def __post_init__(self) -> None:
        _check_positive_int(self.hot_days, "hot_days")
        _check_positive_int(self.warm_days, "warm_days")
        _check_positive_int(self.cold_days, "cold_days")
        if not (self.hot_days < self.warm_days < self.cold_days):
            raise ValueError(
                "boundaries must satisfy hot_days < warm_days < cold_days, got "
                f"{self.hot_days} < {self.warm_days} < {self.cold_days}"
            )
        if not self.warm_volume or not self.cold_volume:
            raise ValueError("warm_volume and cold_volume must be non-empty")

    # --- classification ----------------------------------------------------------------------

    def tier_for_age(self, age: timedelta) -> StorageTier:
        """Classify a span by its age. Negative ages (clock skew) are treated as hot."""
        days = age.total_seconds() / 86_400
        if days < self.hot_days:
            return StorageTier.HOT
        if days < self.warm_days:
            return StorageTier.WARM
        if days < self.cold_days:
            return StorageTier.COLD
        return StorageTier.AGGREGATE

    def tier_at(self, span_ts: datetime, as_of: datetime) -> StorageTier:
        """Classify ``span_ts`` as observed at ``as_of`` (both coerced to UTC)."""
        return self.tier_for_age(_as_utc(as_of) - _as_utc(span_ts))

    def raw_dropped(self, span_ts: datetime, as_of: datetime) -> bool:
        """True once the raw span is past the cold horizon (only the aggregate survives)."""
        return self.tier_at(span_ts, as_of) is StorageTier.AGGREGATE

    # --- TTL DDL generation ------------------------------------------------------------------

    def ttl_actions(self) -> tuple[TtlAction, ...]:
        """The ordered TTL transitions: hot→warm, warm→cold, then drop raw at the cold horizon."""
        return (
            TtlAction(self.hot_days, TtlActionKind.MOVE, self.warm_volume),
            TtlAction(self.warm_days, TtlActionKind.MOVE, self.cold_volume),
            TtlAction(self.cold_days, TtlActionKind.DELETE),
        )

    def render_ttl_clause(self, *, timestamp_column: str = "Timestamp") -> str:
        """Render the full multi-line ClickHouse ``TTL`` clause for this policy."""
        clauses = [a.to_sql(timestamp_column=timestamp_column) for a in self.ttl_actions()]
        return "TTL\n    " + ",\n    ".join(clauses)


DEFAULT_POLICY = TieringPolicy()


# --------------------------------------------------------------------------------------------- #
# Per-tenant overrides (enterprise = longer retention)
# --------------------------------------------------------------------------------------------- #
@runtime_checkable
class TieringPolicyStore(Protocol):
    """Resolves the effective tiering policy for a tenant."""

    def policy_for(self, tenant_id: str) -> TieringPolicy: ...


@dataclass(slots=True)
class InMemoryTieringPolicyStore:
    """Default policy with per-tenant overrides (enterprise tenants retain raw spans longer)."""

    default: TieringPolicy = field(default_factory=lambda: DEFAULT_POLICY)
    overrides: dict[str, TieringPolicy] = field(default_factory=dict)

    def policy_for(self, tenant_id: str) -> TieringPolicy:
        return self.overrides.get(tenant_id, self.default)

    def set_override(self, tenant_id: str, policy: TieringPolicy) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        self.overrides[tenant_id] = policy

    def tier_at(self, tenant_id: str, span_ts: datetime, as_of: datetime) -> StorageTier:
        return self.policy_for(tenant_id).tier_at(span_ts, as_of)


def render_tenant_ttl_delete_expression(
    store: InMemoryTieringPolicyStore,
    *,
    timestamp_column: str = "Timestamp",
    tenant_column: str = "TenantId",
) -> str:
    """Compile per-tenant raw-drop horizons into a single ClickHouse ``multiIf`` DELETE expression.

    ClickHouse TTL is table-level, so a per-tenant override can't be a separate clause. Instead the
    delete interval becomes a ``multiIf`` on ``TenantId`` — overrides first (deterministic order),
    then the default horizon as the fallback branch.
    """
    if not store.overrides:
        action = TtlAction(store.default.cold_days, TtlActionKind.DELETE)
        return action.to_sql(timestamp_column=timestamp_column)

    branches: list[str] = []
    for tenant_id in sorted(store.overrides):
        days = store.overrides[tenant_id].cold_days
        branches.append(f"{tenant_column} = '{tenant_id}', INTERVAL {days} DAY")
    branches.append(f"INTERVAL {store.default.cold_days} DAY")
    inner = ", ".join(branches)
    return f"toDateTime({timestamp_column}) + multiIf({inner}) DELETE"


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC; treat naive datetimes as already-UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
