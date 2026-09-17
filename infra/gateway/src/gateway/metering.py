"""Server-side metering: tamper-evident head counts + billing-period rollups (CTO-84/85/86).

Two billable units (spec §6.1):

* **Billable traces**: counted at ingest HEAD, *before* any sampling decision, so the invoice is
  exact regardless of the analytics sample rate (CTO-84). Sampling analytics down must never reduce
  the billed count.
* **Distinct active feature tags** per tenant per billing period (CTO-85).

Both are **tamper-evident**: each ``(tenant, period)`` carries a content *commitment* (a
collision-resistant hash over the sorted set of distinct ids) that can be recomputed from raw
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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import blake2b

from tally.timekeeping import representable_ts_ns

_NS_PER_S = 1_000_000_000
_TenantPeriod = tuple[str, str]


class ClosedPeriodError(RuntimeError):
    """Raised when attempting to mutate a billing period that has already been closed."""


def billing_period(ts_ns: int) -> str:
    """Return the UTC billing period (``"YYYY-MM"``) a nanosecond timestamp falls in."""
    dt = datetime.fromtimestamp(representable_ts_ns(ts_ns) / _NS_PER_S, tz=timezone.utc)
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


# CTO-401: ceiling on the exact id set kept per ``(tenant, period)``. Nothing evicts these sets
# today (period close is the CTO-213 scheduler's job and does not run on a schedule yet), so an
# unbounded set interns one live string per distinct id for the lifetime of the process: a tenant
# emitting 10M distinct ids in a month is ~10M strings per replica and eventually an OOM on a busy
# tenant.
#
# Default is 2.5x the free tier's 100,000 trace ceiling (see DEFAULT_PLAN_LIMIT). That placement is
# the point: saturation can only happen far ABOVE every plan limit this count is compared against,
# so the over-limit decision the meter feeds is the same before and after the cap. What saturation
# costs is the exact figure, and that loss is declared rather than hidden: see ``record``.
DEFAULT_MAX_IDS_PER_PERIOD = 250_000


@dataclass(slots=True)
class DistinctMeter:
    """Counts distinct ids per ``(tenant, period)`` with a tamper-evident commitment.

    The shared engine behind both the head trace-count meter (CTO-84) and the feature-count meter
    (CTO-85). ``record`` is idempotent per id, so counting the same trace/feature twice (e.g. an
    at-least-once redelivery) never inflates the count.

    Memory is bounded per ``(tenant, period)`` by ``max_ids_per_period`` (CTO-401).
    """

    max_ids_per_period: int = DEFAULT_MAX_IDS_PER_PERIOD
    _ids: dict[_TenantPeriod, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # Buckets that hit the cap. Tracked separately from the ids so a saturated bucket can still
    # answer honestly about itself after it has stopped growing.
    _saturated: set[_TenantPeriod] = field(default_factory=set)

    def record(self, tenant_id: str, ident: str, *, period: str) -> bool:
        """Record ``ident`` for the period. Returns True iff it was newly counted.

        CTO-401: at ``max_ids_per_period`` distinct ids the bucket stops interning and is marked
        SATURATED. This is a stated policy, not silent truncation: past the cap the count stops
        growing and becomes a floor, and the bucket says so (:meth:`exact` is False and
        :meth:`commitment` returns None rather than a digest over a set that is missing members).
        A commitment that reconciles against nothing must never look like one that does, so the
        absence is represented as an absence, exactly as CTO-390 does for an uncomputed one.
        """
        key = (tenant_id, period)
        bucket = self._ids[key]
        if ident in bucket:
            return False
        if len(bucket) >= self.max_ids_per_period:
            self._saturated.add(key)
            return False
        bucket.add(ident)
        return True

    def count(self, tenant_id: str, period: str) -> int:
        return len(self._ids.get((tenant_id, period), ()))

    def exact(self, tenant_id: str, period: str) -> bool:
        """False once the bucket has saturated, i.e. :meth:`count` is a floor, not the figure."""
        return (tenant_id, period) not in self._saturated

    def commitment(self, tenant_id: str, period: str) -> str | None:
        """Commitment over the distinct ids, or None once the bucket has saturated (CTO-401)."""
        if not self.exact(tenant_id, period):
            return None
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


def validate_max_ids_per_period(
    max_ids_per_period: int,
    *,
    plan_limits: Iterable[PlanLimit] = (DEFAULT_PLAN_LIMIT,),
) -> None:
    """Refuse a meter cap that cannot decide the overage it is measured against (CTO-401 review).

    The cap makes the count a FLOOR once a bucket saturates. That is honest and survivable while the
    cap sits far above every plan ceiling, because a floor above the ceiling still proves the
    overage. It stops being either the moment the cap drops to or below a ceiling: saturation then
    pins the count AT OR BELOW the limit forever, ``trace_count > trace_limit`` can never become
    true, and limit enforcement is silently off for every tenant on that plan. A cap of 0 is the
    extreme of the same bug, a head meter that counts nothing while reporting a clean zero.

    Nothing bounded this knob before, so an operator trimming replica memory could turn enforcement
    off across the fleet with no error, no warning and no visible change in the numbers. Strictly
    greater than the largest ceiling rather than equal to it: at equality the count saturates exactly
    at the limit and the strict ``>`` comparison the overage uses is unreachable.

    Raises :class:`ValueError`, which the gateway lifespan turns into a refusal to start.
    """
    if max_ids_per_period < 1:
        raise ValueError(
            f"metering_max_ids_per_period must be >= 1, got {max_ids_per_period}: a cap of 0 "
            "disables the head meter entirely while it goes on reporting a count of 0 "
            "(TALLY_METERING_MAX_IDS_PER_PERIOD)"
        )
    ceilings = [p.trace_limit for p in plan_limits if p.trace_limit is not None]
    largest = max(ceilings, default=0)
    if max_ids_per_period <= largest:
        raise ValueError(
            f"metering_max_ids_per_period ({max_ids_per_period}) is not above the largest "
            f"configured plan trace_limit ({largest}), so a saturated period can never exceed its "
            "limit and limit enforcement would be silently disabled for those tenants. Raise "
            "TALLY_METERING_MAX_IDS_PER_PERIOD above the largest plan ceiling"
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Per-tenant per-period usage, the unit the dashboard and billing both consume (CTO-86)."""

    tenant_id: str
    period: str
    trace_count: int
    feature_count: int
    # CTO-390: ``None`` means "not computed", which is what a count derived from a ClickHouse
    # aggregate honestly has to say. The commitment is a hash over the whole distinct id set, and
    # producing it for an open period would mean materialising every id. An empty string here would
    # be a commitment that reconciles against nothing while looking exactly like one that does, so
    # the absence is represented as an absence.
    trace_commitment: str | None
    feature_commitment: str | None
    plan: str
    trace_limit: int | None
    feature_limit: int | None
    closed: bool
    # CTO-401: False when the meter's id set for this period saturated its cap, which makes the
    # matching count a FLOOR rather than the figure. Default True: every other producer of a
    # UsageRecord (the durable ClickHouse/Postgres path, CTO-390) counts exactly, and a reader must
    # not have to guess which kind of number it is holding.
    trace_count_exact: bool = True
    feature_count_exact: bool = True

    @property
    def over_trace_limit(self) -> bool | None:
        """True / False / None, where None means "the count is a floor and it has not passed yet".

        CTO-401 review. This used to compute a confident bool from a count it knew might be a floor.
        A saturated period under its limit answered False, which reads as "this tenant is within
        their plan" when the truth is "we stopped counting and cannot say". That is the
        honest-under-uncertainty invariant broken in the exact way it names: an unknown rendered as a
        definite answer. Note the asymmetry, which is what keeps this useful rather than merely
        cautious:

          * OVER is still decidable from a floor. If the floor already exceeds the limit, the real
            count exceeds it too, so True is safe.
          * UNDER is not. A floor below the limit says nothing about where the real count sits.

        So only the under-and-inexact case becomes None. An exact count answers False as before, and
        an unlimited plan answers False because unlimited genuinely cannot be exceeded.
        """
        if self.trace_limit is None:
            return False
        if self.trace_count > self.trace_limit:
            return True
        return False if self.trace_count_exact else None

    @property
    def over_feature_limit(self) -> bool | None:
        """As :attr:`over_trace_limit`, for the distinct feature-tag ceiling (CTO-85)."""
        if self.feature_limit is None:
            return False
        if self.feature_count > self.feature_limit:
            return True
        return False if self.feature_count_exact else None

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
            "trace_count_exact": self.trace_count_exact,
            "feature_count_exact": self.feature_count_exact,
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
        max_ids_per_period: int = DEFAULT_MAX_IDS_PER_PERIOD,
    ) -> None:
        # CTO-401: both meters are bounded. The feature meter is naturally small (distinct tags),
        # but it is fed by client-supplied strings, so it gets the same ceiling rather than relying
        # on a tenant's good behaviour to stay small.
        self._traces = DistinctMeter(max_ids_per_period=max_ids_per_period)
        self._features = DistinctMeter(max_ids_per_period=max_ids_per_period)
        self._closed: dict[_TenantPeriod, UsageRecord] = {}
        self._limits: dict[str, PlanLimit] = {}
        self._default_limit = default_limit
        self._now_ns = now_ns if callable(now_ns) else time.time_ns

    # --- plan limits -------------------------------------------------------------------------

    def set_plan(self, tenant_id: str, limit: PlanLimit) -> None:
        self._limits[tenant_id] = limit

    def _limit_for(self, tenant_id: str) -> PlanLimit:
        return self._limits.get(tenant_id, self._default_limit)

    def plan_limit_for(self, tenant_id: str) -> PlanLimit:
        """Public read of a tenant's ceilings, for callers that count elsewhere (CTO-390).

        The durable usage path derives its counts from ClickHouse and Postgres but still needs the
        plan those counts are measured against. Plan limits are configuration, not telemetry, so
        they stay here rather than being duplicated into a second owner that could drift.
        """
        return self._limit_for(tenant_id)

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
        trace_id_synthetic: bool = False,
    ) -> None:
        """Meter one span at HEAD: count its (distinct) trace, and its feature tag if present.

        Empty/None ids are ignored. This is intentionally called *before* any sampling or
        backpressure shed so neither can reduce the billable count.

        The ``trace_id_synthetic`` flag is CLIENT-ASSERTED, NOT CORROBORATED: it arrives on the wire
        and the gateway cannot verify it, because a minted id and a real one are both random hex and
        differ in nothing but this claim. Suppressing a count on an unverified client assertion is
        safe while the head meter only feeds a usage display and a plan-limit check, and is NOT safe
        the moment it feeds an invoice, where it becomes a field a client can set to bill themselves
        for nothing. Wiring this meter to billing requires corroboration first: see CTO-410, which
        blocks CTO-399.

        CTO-401: a SYNTHETIC trace id is not a billable trace. CTO-396 started stamping a fresh
        trace id on every span an SDK emitted outside a ``start_trace``, so spans that used to reach
        this method with ``trace_id=None`` (and were skipped by the guard below) now arrive with a
        unique id each and were counted one billable trace per span. That is a per-span meter
        wearing a per-trace name, and on the free tier's 100,000-trace ceiling it turns a tenant who
        contributed zero into one who contributes one per span. The id is still written to
        ClickHouse, so the invoice count derived from stored spans (CTO-390) is untouched; only the
        head meter's notion of a trace reverts to what it was before CTO-396. The feature tag is
        metered normally: it is real regardless of how the span's trace id came about.
        """
        if trace_id and not trace_id_synthetic:
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
            trace_count_exact=self._traces.exact(tenant_id, period),
            feature_count_exact=self._features.exact(tenant_id, period),
        )
