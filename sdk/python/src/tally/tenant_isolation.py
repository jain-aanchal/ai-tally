# SPDX-License-Identifier: Apache-2.0
"""Multi-tenant isolation — query admission, tenant-scope enforcement, resource caps (CTO-30).

A shared multi-tenant cluster (CTO-18) has one non-negotiable rule: **one tenant must never starve
another and must never read another's data** (spec §11). This module is the pure-logic layer that
enforces it above the storage engine:

* :class:`QueryConcurrencyLimiter` — caps the number of in-flight *expensive* queries per tenant
  (default 4) so a single tenant's heavy dashboard can't monopolize the cluster.
* :class:`TenantQueryGuard` — refuses any query that isn't scoped to exactly the requesting tenant.
  An unscoped query (no tenant predicate) would scan the whole cluster; a query referencing another
  tenant is a data-isolation breach. Both are blocked (the negative-test guarantee).
* :func:`resource_group_for` — generates the per-tenant ClickHouse Resource Group settings
  (memory + concurrency + CPU weight). The cluster applies them; this is the source of the values.
* :func:`rank_heavy_tenants` / :func:`recommend_shard_promotions` — the documented shard-promotion
  path: rank tenants by load and surface the top-N that should be promoted off the shared shard.

Cluster-side application (real ClickHouse Resource Groups) and shard-migration tooling are infra /
follow-ups; this module owns the policy and the decisions.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

DEFAULT_MAX_CONCURRENT_QUERIES = 4


# --------------------------------------------------------------------------------------------- #
# Per-tenant query concurrency limiting (AC: default 4 concurrent expensive queries)
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """Result of asking the limiter for a query slot."""

    admitted: bool
    tenant_id: str
    in_flight: int
    limit: int
    reason: str | None = None


class QueryAdmissionError(RuntimeError):
    """Raised by :meth:`QueryConcurrencyLimiter.lease` when a tenant is at its concurrency cap."""

    def __init__(self, outcome: AdmissionOutcome) -> None:
        super().__init__(
            f"tenant {outcome.tenant_id!r} at concurrency limit "
            f"({outcome.in_flight}/{outcome.limit})"
        )
        self.outcome = outcome


class QueryConcurrencyLimiter:
    """Thread-safe per-tenant in-flight query counter with a hard cap.

    Limits are per tenant and independent — one tenant hitting its cap never affects another.
    """

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT_QUERIES) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            raise ValueError("max_concurrent must be an int")
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._limit = max_concurrent
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        self._overrides: dict[str, int] = {}

    def set_limit(self, tenant_id: str, limit: int) -> None:
        """Override the concurrency cap for a single tenant (e.g. enterprise gets more)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive int")
        with self._lock:
            self._overrides[tenant_id] = limit

    def limit_for(self, tenant_id: str) -> int:
        return self._overrides.get(tenant_id, self._limit)

    def in_flight(self, tenant_id: str) -> int:
        with self._lock:
            return self._in_flight.get(tenant_id, 0)

    def try_acquire(self, tenant_id: str) -> AdmissionOutcome:
        """Reserve a slot if under the cap. Never raises — returns an outcome."""
        if not tenant_id:
            return AdmissionOutcome(False, tenant_id, 0, 0, reason="missing tenant_id")
        with self._lock:
            limit = self._overrides.get(tenant_id, self._limit)
            current = self._in_flight.get(tenant_id, 0)
            if current >= limit:
                return AdmissionOutcome(
                    False, tenant_id, current, limit, reason="at concurrency limit"
                )
            self._in_flight[tenant_id] = current + 1
            return AdmissionOutcome(True, tenant_id, current + 1, limit)

    def release(self, tenant_id: str) -> None:
        """Free a previously-acquired slot. Idempotent below zero (never goes negative)."""
        with self._lock:
            current = self._in_flight.get(tenant_id, 0)
            if current <= 1:
                self._in_flight.pop(tenant_id, None)
            else:
                self._in_flight[tenant_id] = current - 1

    @contextmanager
    def lease(self, tenant_id: str) -> Iterator[AdmissionOutcome]:
        """Acquire-or-raise context manager; releases the slot on exit."""
        outcome = self.try_acquire(tenant_id)
        if not outcome.admitted:
            raise QueryAdmissionError(outcome)
        try:
            yield outcome
        finally:
            self.release(tenant_id)


# --------------------------------------------------------------------------------------------- #
# Tenant-scope enforcement (AC: cross-tenant query impossible/blocked)
# --------------------------------------------------------------------------------------------- #
class CrossTenantAccessError(PermissionError):
    """Raised when a query references a tenant other than the requester, or isn't tenant-scoped."""


@dataclass(frozen=True, slots=True)
class ScopedQuery:
    """A query annotated with the requester and the tenant(s) its predicates reference."""

    requester_tenant_id: str
    referenced_tenant_ids: frozenset[str]
    sql: str = ""

    def __post_init__(self) -> None:
        if not self.requester_tenant_id:
            raise ValueError("requester_tenant_id must be non-empty")


class TenantQueryGuard:
    """Blocks any query not scoped to exactly the requesting tenant."""

    def violation(self, query: ScopedQuery) -> str | None:
        """Return a reason string if the query is unsafe, else None."""
        refs = query.referenced_tenant_ids
        if not refs:
            return "query is not tenant-scoped (no tenant predicate)"
        foreign = refs - {query.requester_tenant_id}
        if foreign:
            return f"query references foreign tenants: {sorted(foreign)}"
        return None

    def is_safe(self, query: ScopedQuery) -> bool:
        return self.violation(query) is None

    def check(self, query: ScopedQuery) -> None:
        """Raise :class:`CrossTenantAccessError` if the query would breach isolation."""
        reason = self.violation(query)
        if reason is not None:
            raise CrossTenantAccessError(reason)


# --------------------------------------------------------------------------------------------- #
# Per-tenant resource groups (AC: memory + CPU caps configured per tenant)
# --------------------------------------------------------------------------------------------- #
class IsolationTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


_GIB = 1024**3

# Caps per tier: (max_memory_bytes, max_concurrent_queries, cpu_weight).
_TIER_CAPS: dict[IsolationTier, tuple[int, int, int]] = {
    IsolationTier.FREE: (2 * _GIB, 2, 50),
    IsolationTier.PRO: (8 * _GIB, 4, 100),
    IsolationTier.ENTERPRISE: (32 * _GIB, 16, 300),
}


@dataclass(frozen=True, slots=True)
class ResourceGroup:
    """Per-tenant ClickHouse Resource Group caps (the values the cluster enforces)."""

    tenant_id: str
    max_memory_bytes: int
    max_concurrent_queries: int
    cpu_weight: int

    def as_settings(self) -> dict[str, object]:
        """ClickHouse settings-profile shape for this tenant."""
        return {
            "profile": f"tenant_{self.tenant_id}",
            "max_memory_usage": self.max_memory_bytes,
            "max_concurrent_queries_for_user": self.max_concurrent_queries,
            "priority": self.cpu_weight,
        }


def resource_group_for(tenant_id: str, tier: IsolationTier = IsolationTier.PRO) -> ResourceGroup:
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    mem, conc, cpu = _TIER_CAPS[tier]
    return ResourceGroup(tenant_id, mem, conc, cpu)


# --------------------------------------------------------------------------------------------- #
# Shard-promotion path (AC: documented path for top-N heavy tenants)
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TenantLoad:
    """Observed load for a tenant over a window (drives the shard-promotion decision)."""

    tenant_id: str
    queries_per_day: int
    bytes_scanned_per_day: int

    @property
    def load_score(self) -> float:
        """A single comparable load number: scan volume dominates, query rate breaks ties.

        bytes_scanned (in GiB) is the cluster-pressure signal; query count is secondary.
        """
        return self.bytes_scanned_per_day / _GIB + self.queries_per_day / 1000.0


# Default: a tenant scanning > ~5 TiB/day of cluster reads is a shard-promotion candidate.
DEFAULT_PROMOTION_SCORE = 5 * 1024.0


def rank_heavy_tenants(loads: Iterable[TenantLoad], *, top_n: int = 10) -> tuple[TenantLoad, ...]:
    """Heaviest tenants first (by load score, tenant_id as a stable tiebreaker)."""
    ordered = sorted(loads, key=lambda t: (-t.load_score, t.tenant_id))
    return tuple(ordered[:top_n])


def recommend_shard_promotions(
    loads: Iterable[TenantLoad], *, threshold_score: float = DEFAULT_PROMOTION_SCORE
) -> tuple[str, ...]:
    """Tenant ids whose load exceeds the promotion threshold — promote them off the shared shard.

    The documented promotion path: (1) flag here, (2) provision a dedicated shard, (3) dual-write +
    backfill, (4) cut reads over, (5) drop from the shared shard. Steps 2–5 are migration tooling
    (out of scope per CTO-30); this function owns step 1, the decision.
    """
    candidates = [t for t in loads if t.load_score >= threshold_score]
    candidates.sort(key=lambda t: (-t.load_score, t.tenant_id))
    return tuple(t.tenant_id for t in candidates)
