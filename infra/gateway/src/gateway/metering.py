"""Server-side metering — tamper-evident head counts + billing-period rollups (CTO-84/85/86).

Two billable units (spec §6.1):

* **Billable traces** — counted at ingest HEAD, *before* any sampling decision, so the invoice is
  exact regardless of the analytics sample rate (CTO-84). Sampling analytics down must never reduce
  the billed count.
* **Distinct active feature tags** per tenant per billing period (CTO-85).

Both are **tamper-evident**: each ``(tenant, period)`` carries a content *commitment* — a
collision-resistant hash over the sorted set of distinct ids — that can be recomputed from raw
ingest to detect dropped or injected records. **Closed periods are immutable** (CTO-86): once a
billing period is closed its usage record is frozen and further records for it are rejected.

This module is pure logic (no storage). A production deployment backs the distinct sets with a
ClickHouse aggregating projection / ``uniqExact``; the commitment is what lets billing reconcile
that authoritative store against raw ingest. The in-memory distinct sets here keep the core
unit-testable and are the reference semantics the store must match.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import blake2b

_NS_PER_S = 1_000_000_000
_TenantPeriod = tuple[str, str]


class ClosedPeriodError(RuntimeError):
    """Raised when attempting to mutate a billing period that has already been closed."""


def billing_period(ts_ns: int) -> str:
    """Return the UTC billing period (``"YYYY-MM"``) a nanosecond timestamp falls in."""
    dt = datetime.fromtimestamp(ts_ns / _NS_PER_S, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def commitment(ids: set[str]) -> str:
    """Order-independent, collision-resistant commitment over a set of distinct ids.

    Sorting makes it independent of ingest order; hashing the joined, delimited ids makes a dropped
    or injected id change the digest. Recomputing this over the distinct ids found in raw ingest and
    comparing to the stored value is how a closed period is reconciled / proven untampered.
    """
    h = blake2b(digest_size=16)
    for ident in sorted(ids):
        h.update(ident.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(slots=True)
class DistinctMeter:
    """Counts distinct ids per ``(tenant, period)`` with a tamper-evident commitment.

    The shared engine behind both the head trace-count meter (CTO-84) and the feature-count meter
    (CTO-85). ``record`` is idempotent per id, so counting the same trace/feature twice — e.g. an
    at-least-once redelivery — never inflates the count.
    """

    _ids: dict[_TenantPeriod, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )

    def record(self, tenant_id: str, ident: str, *, period: str) -> bool:
        """Record ``ident`` for the period. Returns True iff it was newly counted."""
        bucket = self._ids[(tenant_id, period)]
        if ident in bucket:
            return False
        bucket.add(ident)
        return True

    def count(self, tenant_id: str, period: str) -> int:
        return len(self._ids.get((tenant_id, period), ()))

    def commitment(self, tenant_id: str, period: str) -> str:
        return commitment(self._ids.get((tenant_id, period), set()))

    def periods(self, tenant_id: str) -> set[str]:
        return {p for (t, p) in self._ids if t == tenant_id}


@dataclass(frozen=True, slots=True)
class PlanLimit:
    """A tenant's billable ceilings for a period. ``None`` means unlimited."""

    plan: str = "free"
    trace_limit: int | None = None
    feature_limit: int | None = None


# Conservative default until a tenant's plan is set explicitly (real tiers land in CTO-89).
DEFAULT_PLAN_LIMIT = PlanLimit(plan="free", trace_limit=100_000, feature_limit=25)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Per-tenant per-period usage — the unit the dashboard and billing both consume (CTO-86)."""

    tenant_id: str
    period: str
    trace_count: int
    feature_count: int
    trace_commitment: str
    feature_commitment: str
    plan: str
    trace_limit: int | None
    feature_limit: int | None
    closed: bool

    @property
    def over_trace_limit(self) -> bool:
        return self.trace_limit is not None and self.trace_count > self.trace_limit

    @property
    def over_feature_limit(self) -> bool:
        return self.feature_limit is not None and self.feature_count > self.feature_limit

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "period": self.period,
            "plan": self.plan,
            "trace_count": self.trace_count,
            "feature_count": self.feature_count,
            "trace_limit": self.trace_limit,
            "feature_limit": self.feature_limit,
            "over_trace_limit": self.over_trace_limit,
            "over_feature_limit": self.over_feature_limit,
            "trace_commitment": self.trace_commitment,
            "feature_commitment": self.feature_commitment,
            "closed": self.closed,
        }


class UsageRollup:
    """Rolls the trace + feature meters into per-tenant per-period :class:`UsageRecord`s (CTO-86).

    The ingest pipeline calls :meth:`record_span` at HEAD (before the sampling/shed decision) so the
    billed trace count is independent of the analytics sample rate. The usage API reads :meth:`usage`
    to show current-period usage vs. plan limit. :meth:`close_period` freezes a period: its snapshot
    becomes immutable and any later record for it raises :class:`ClosedPeriodError`.
    """

    def __init__(
        self,
        *,
        default_limit: PlanLimit = DEFAULT_PLAN_LIMIT,
        now_ns: object | None = None,
    ) -> None:
        self._traces = DistinctMeter()
        self._features = DistinctMeter()
        self._closed: dict[_TenantPeriod, UsageRecord] = {}
        self._limits: dict[str, PlanLimit] = {}
        self._default_limit = default_limit
        self._now_ns = now_ns if callable(now_ns) else time.time_ns

    # --- plan limits -------------------------------------------------------------------------

    def set_plan(self, tenant_id: str, limit: PlanLimit) -> None:
        self._limits[tenant_id] = limit

    def _limit_for(self, tenant_id: str) -> PlanLimit:
        return self._limits.get(tenant_id, self._default_limit)

    # --- metering (HEAD path) ----------------------------------------------------------------

    def record_trace(self, tenant_id: str, trace_id: str, ts_ns: int) -> bool:
        period = billing_period(ts_ns)
        self._guard_open(tenant_id, period)
        return self._traces.record(tenant_id, trace_id, period=period)

    def record_feature(self, tenant_id: str, feature_tag: str, ts_ns: int) -> bool:
        period = billing_period(ts_ns)
        self._guard_open(tenant_id, period)
        return self._features.record(tenant_id, feature_tag, period=period)

    def record_span(
        self,
        tenant_id: str,
        *,
        trace_id: str | None,
        feature_tag: str | None,
        ts_ns: int,
    ) -> None:
        """Meter one span at HEAD: count its (distinct) trace, and its feature tag if present.

        Empty/None ids are ignored. This is intentionally called *before* any sampling or
        backpressure shed so neither can reduce the billable count.
        """
        if trace_id:
            self.record_trace(tenant_id, trace_id, ts_ns)
        if feature_tag:
            self.record_feature(tenant_id, feature_tag, ts_ns)

    def _guard_open(self, tenant_id: str, period: str) -> None:
        if (tenant_id, period) in self._closed:
            raise ClosedPeriodError(f"billing period {period} for {tenant_id} is closed")

    # --- rollups / usage API -----------------------------------------------------------------

    def usage(self, tenant_id: str, period: str | None = None) -> UsageRecord:
        """Current usage for ``period`` (defaults to the tenant's *current* period)."""
        if period is None:
            period = billing_period(self._now_ns())
        closed = self._closed.get((tenant_id, period))
        if closed is not None:
            return closed
        return self._snapshot(tenant_id, period, closed=False)

    def close_period(self, tenant_id: str, period: str) -> UsageRecord:
        """Freeze a period: returns (and caches) an immutable snapshot. Idempotent."""
        existing = self._closed.get((tenant_id, period))
        if existing is not None:
            return existing
        record = self._snapshot(tenant_id, period, closed=True)
        self._closed[(tenant_id, period)] = record
        return record

    def _snapshot(self, tenant_id: str, period: str, *, closed: bool) -> UsageRecord:
        limit = self._limit_for(tenant_id)
        return UsageRecord(
            tenant_id=tenant_id,
            period=period,
            trace_count=self._traces.count(tenant_id, period),
            feature_count=self._features.count(tenant_id, period),
            trace_commitment=self._traces.commitment(tenant_id, period),
            feature_commitment=self._features.commitment(tenant_id, period),
            plan=limit.plan,
            trace_limit=limit.trace_limit,
            feature_limit=limit.feature_limit,
            closed=closed,
        )
