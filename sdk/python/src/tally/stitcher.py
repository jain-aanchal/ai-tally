"""Attribution stitcher — bridge async business events to traces.

Implements CTO-69 / spec §7.

The stitcher is intentionally a pure library here: it operates on in-memory inputs (touches,
identity edges, events) so it's testable without ClickHouse. In production the lookups are
backed by ``last_touch_index`` / ``identity_graph`` tables (CTO-25/26); a backend implementing
:class:`TouchStore` is the only thing that changes.

Algorithm (per spec §7.1):

1. Resolve the event's user-hash to its identity set via transitive lookup in
   :class:`IdentityGraph` (bounded depth, default 2). This bridges anonymous→authenticated,
   cross-device, and HMAC key versions (CTO-74).
2. For each configured ``AttributionRule`` (one per `(event_name, feature_tag)`):
   look up the most recent touch within the lookback window from any hash in the identity set.
3. Decide the attribution confidence (direct / session_stitched / identity_graph_stitched).
4. Upsert an idempotent :class:`AttributionRecord` keyed on
   ``(tenant_id, business_event_id, feature_tag)``.
5. If no candidate touch is found, emit an :class:`UnattributedRecord` (queryable, never silent).
6. On a late identity edge, re-run for previously unattributed events involving the edge's
   endpoints (:func:`restitch_on_new_edge`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

# Identity graph primitives are now a first-class component (CTO-67); re-exported here so the
# stitcher's public surface (and its tests) keep importing them from ``tally.stitcher``.
from tally.identity import (
    IdentityEdge,
    IdentityGraph,
    IdentityType,
)

__all__ = [
    "IdentityEdge",
    "IdentityGraph",
    "IdentityType",
]

# --- types ---------------------------------------------------------------------------------------


class AttributionConfidence(str, Enum):
    DIRECT = "direct"
    SESSION_STITCHED = "session_stitched"
    IDENTITY_GRAPH_STITCHED = "identity_graph_stitched"


class ValueType(str, Enum):
    MONETARY = "monetary"
    COUNT = "count"
    MRR = "mrr"
    REFUND = "refund"


@dataclass(frozen=True, slots=True)
class Touch:
    """A trace's most recent attributable touch (matches ``last_touch_index`` rows)."""

    trace_id: str
    user_hash: str
    feature_tag: str
    ts: datetime
    cost_micro_usd: int
    key_version: str = "v1"


@dataclass(frozen=True, slots=True)
class BusinessEvent:
    business_event_id: str
    tenant_id: str
    user_hash: str
    event_name: str
    occurred_at: datetime
    value_amount_micro: int | None = None
    value_currency: str = "USD"
    value_type: ValueType = ValueType.MONETARY
    source: str = "sdk"


@dataclass(frozen=True, slots=True)
class AttributionRule:
    """Per-tenant config: which value events to attribute to which features, with what window."""

    event_name: str
    feature_tag: str
    lookback_days: int = 30
    attribution_model: str = "last_touch_v1"


@dataclass(frozen=True, slots=True)
class AttributionRecord:
    tenant_id: str
    business_event_id: str
    feature_tag: str
    attributed_trace_id: str
    attributed_trace_ts: datetime
    attributed_trace_cost_micro_usd: int
    value_amount_micro: int | None
    value_currency: str
    value_type: ValueType
    attribution_model: str
    confidence: AttributionConfidence
    user_id_hash_key_version: str
    lookback_window_days: int
    stitched_at: datetime
    stitcher_version: str = "v1"


@dataclass(frozen=True, slots=True)
class UnattributedRecord:
    tenant_id: str
    business_event_id: str
    event_name: str
    user_hash: str
    occurred_at: datetime
    reason: str  # 'no_trace_in_window' | 'feature_tag_no_rule' | etc.
    last_checked_at: datetime


# --- touch store ---------------------------------------------------------------------------------


class TouchStore(Protocol):
    """Backend interface for "most recent touch per (user, feature) within a window".

    The :class:`MemoryTouchStore` below is the test/dev implementation; ClickHouse-backed
    implementations live in the gateway and read from ``last_touch_index`` (CTO-25).
    """

    def query_last_touch(
        self,
        *,
        tenant_id: str,
        user_hashes: set[str],
        feature_tag: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Touch | None: ...


@dataclass(slots=True)
class MemoryTouchStore:
    """In-memory store: each ``add_touch`` keeps the newest touch per (tenant, user, feature)."""

    _by_key: dict[tuple[str, str, str], Touch] = field(default_factory=dict)

    def add_touch(self, tenant_id: str, touch: Touch) -> None:
        key = (tenant_id, touch.user_hash, touch.feature_tag)
        cur = self._by_key.get(key)
        if cur is None or touch.ts > cur.ts:
            self._by_key[key] = touch

    def query_last_touch(
        self,
        *,
        tenant_id: str,
        user_hashes: set[str],
        feature_tag: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Touch | None:
        best: Touch | None = None
        for u in user_hashes:
            t = self._by_key.get((tenant_id, u, feature_tag))
            if t is None:
                continue
            if t.ts < window_start or t.ts > window_end:
                continue
            if best is None or t.ts > best.ts:
                best = t
        return best


# --- stitcher ------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stitcher:
    """Per-tenant stitcher. Holds rules + identity graph + touches.

    Idempotent on ``(tenant_id, business_event_id, feature_tag)`` — re-stitching produces an
    upsert in :attr:`records`, not a duplicate.
    """

    identity_graph: IdentityGraph = field(default_factory=IdentityGraph)
    touches: TouchStore = field(default_factory=MemoryTouchStore)
    rules: list[AttributionRule] = field(default_factory=list)
    records: dict[tuple[str, str, str], AttributionRecord] = field(default_factory=dict)
    unattributed: dict[tuple[str, str], UnattributedRecord] = field(default_factory=dict)
    identity_resolve_max_depth: int = 2

    def stitch(
        self,
        event: BusinessEvent,
        *,
        now: datetime | None = None,
        identity_as_of: datetime | None = None,
    ) -> list[AttributionRecord]:
        """Stitch one event against all configured rules. Returns the records produced/updated.

        Per-feature: a single event can attribute to multiple features (one record each). The UI
        is explicit that summing across features double-counts at the conversion level (spec §7.2).

        ``identity_as_of`` bounds which identity edges are eligible. Defaults to
        ``event.occurred_at`` (initial stitch — don't leak edges observed after the event). Pass
        ``now`` from :func:`restitch_on_new_edge` so a late edge retroactively reveals identity.
        """
        now = now or event.occurred_at
        as_of = identity_as_of if identity_as_of is not None else event.occurred_at
        applicable = [r for r in self.rules if r.event_name == event.event_name]

        if not applicable:
            # Not a value event for this tenant — ignore (not an unattributed-with-reason).
            return []

        identity_set = self.identity_graph.resolve(
            event.tenant_id,
            event.user_hash,
            max_depth=self.identity_resolve_max_depth,
            as_of=as_of,
        )

        out: list[AttributionRecord] = []
        for rule in applicable:
            window_start = event.occurred_at - timedelta(days=rule.lookback_days)
            candidate = self.touches.query_last_touch(
                tenant_id=event.tenant_id,
                user_hashes=identity_set,
                feature_tag=rule.feature_tag,
                window_start=window_start,
                window_end=event.occurred_at,  # use occurred_at, not ingested_at
            )

            if candidate is None:
                self.unattributed[(event.tenant_id, event.business_event_id)] = UnattributedRecord(
                    tenant_id=event.tenant_id,
                    business_event_id=event.business_event_id,
                    event_name=event.event_name,
                    user_hash=event.user_hash,
                    occurred_at=event.occurred_at,
                    reason="no_trace_in_window",
                    last_checked_at=now,
                )
                continue

            confidence = self._confidence(event, candidate)

            record = AttributionRecord(
                tenant_id=event.tenant_id,
                business_event_id=event.business_event_id,
                feature_tag=rule.feature_tag,
                attributed_trace_id=candidate.trace_id,
                attributed_trace_ts=candidate.ts,
                attributed_trace_cost_micro_usd=candidate.cost_micro_usd,
                value_amount_micro=event.value_amount_micro,
                value_currency=event.value_currency,
                value_type=event.value_type,
                attribution_model=rule.attribution_model,
                confidence=confidence,
                user_id_hash_key_version=candidate.key_version,
                lookback_window_days=rule.lookback_days,
                stitched_at=now,
            )
            self.records[(event.tenant_id, event.business_event_id, rule.feature_tag)] = record
            self.unattributed.pop((event.tenant_id, event.business_event_id), None)
            out.append(record)

        return out

    def _confidence(self, event: BusinessEvent, candidate: Touch) -> AttributionConfidence:
        if candidate.user_hash == event.user_hash:
            return AttributionConfidence.DIRECT
        direct_neighbour_type = self.identity_graph.edge_type_between(
            event.tenant_id, event.user_hash, candidate.user_hash
        )
        if direct_neighbour_type is IdentityType.SESSION_ID:
            return AttributionConfidence.SESSION_STITCHED
        # one-hop via session also stitched as session_stitched? Spec keeps it bucketed by source:
        # if the direct edge between the two was session, call it session-stitched; otherwise it's
        # via the identity graph (anonymous->user, cross-device, key-version bridge, ...).
        return AttributionConfidence.IDENTITY_GRAPH_STITCHED


# --- re-stitch on late identity edge -------------------------------------------------------------


def restitch_on_new_edge(
    stitcher: Stitcher,
    edge: IdentityEdge,
    pending_events: Iterable[BusinessEvent],
    *,
    tenant_id: str,
    now: datetime | None = None,
) -> list[AttributionRecord]:
    """When a new identity edge lands, re-run stitch for unattributed events whose user_hash
    is now connected to ``edge.a`` or ``edge.b``.

    ``pending_events`` is the set of stored business events for the tenant — typically the rows in
    ``business_events`` whose ids appear in :attr:`Stitcher.unattributed`.
    """
    stitcher.identity_graph.add_edge(tenant_id, edge)
    # If caller didn't supply a clock, anchor on the edge's observed_at (the moment we learned
    # these identities are the same). Any edge with observed_at <= this is eligible.
    effective_now = now or edge.observed_at

    seeds = {edge.a, edge.b}
    out: list[AttributionRecord] = []
    for event in pending_events:
        if event.tenant_id != tenant_id:
            continue
        if (event.tenant_id, event.business_event_id) not in stitcher.unattributed:
            continue
        identity_set = stitcher.identity_graph.resolve(
            tenant_id,
            event.user_hash,
            max_depth=stitcher.identity_resolve_max_depth,
            as_of=effective_now,
        )
        if identity_set.isdisjoint(seeds):
            continue
        # Re-stitch eligibility extends to effective_now so the new edge applies retroactively.
        out.extend(stitcher.stitch(event, now=effective_now, identity_as_of=effective_now))
    return out
