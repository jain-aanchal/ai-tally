# SPDX-License-Identifier: Apache-2.0
"""The attribution stitcher runner (CTO-200): the thing that finally POPULATES attribution_records.

WHY this exists. ``attribution_records`` has 0 rows for every tenant, so ``/features`` renders honest
nulls for value, payback and attribution rate, and always has. The cause was never a broken stitcher:
``sdk/python/src/tally/stitcher.py`` is a pure in-memory library with no ClickHouse-backed
``TouchStore``, no worker calling it, and no writer of ``attribution_records``. The raw signal is
healthy (the demo tenant has ~138k rows in ``last_touch_index``); nothing consumed it. This module is
that missing runner. It is the third scheduled job, alongside the cost connectors (CTO-215) and the
ingest workers / reconciler (CTO-216), and it is deliberately thin: all the attribution LOGIC already
lives and is tested in :mod:`tally.stitcher`. What was missing was the three pieces of plumbing this
file supplies.

WHAT THIS DOES, precisely:

1. :class:`ClickHouseTouchStore` implements the exact :class:`tally.stitcher.TouchStore` protocol the
   stitcher expects ("most recent touch per (user, feature) within a window"), backed by
   ``last_touch_index``.
2. :class:`ClickHouseValueEventSource` reads the tenant's value events from ``business_events`` for
   the same bounded window and maps each row to a :class:`tally.stitcher.BusinessEvent`.
3. A default :class:`tally.stitcher.AttributionRule` set (last-touch, 30-day lookback) generated from
   the feature tags and event names actually present for the tenant, so no per-tenant config row is
   required for the slice to work. A per-tenant config TABLE is a follow-up (see below).
4. :class:`ClickHouseAttributionWriter` writes the produced records back to ``attribution_records``
   (and the unattributable ones to ``unattributed_events``), matching the DDL in
   ``db/clickhouse/attribution.sql``.

THE DEFAULT RULE, and why the rule set is a product of what is present. A
:class:`~tally.stitcher.AttributionRule` is per ``(event_name, feature_tag)``: the stitcher, for each
rule matching an event's name, looks up the last touch for THAT feature tag and, if one exists in the
window, credits the conversion to it. Last-touch attribution therefore means "credit the conversion
to every feature the converting user touched within the lookback window", which is exactly what the
``/features`` per-feature economics query sums (and it is explicit that summing across features
double-counts at the conversion level, spec 7.2). So the default rule set is the cartesian product of
the value event names in ``business_events`` and the feature tags in ``last_touch_index`` for the
tenant, each with the default 30-day window and ``last_touch_v1`` model. Both sides come from the same
two bounded loads the job already does, so generating the rules costs no extra query.

CONFIDENCE. v1 attributes on a DIRECT user-hash match only: the stitcher is handed an empty
:class:`tally.identity.IdentityGraph`, so a converting user's identity set is just their own hash and
every record lands as ``direct``. That is honest and it is what lights ``/features`` up for a tenant
whose revenue connector hashes into the same ``UserIdHash`` space the SDK uses (CTO-110). Populating
the identity graph from the ``identity_graph`` table to earn ``session_stitched`` /
``identity_graph_stitched`` confidence is a follow-up, called out with the late-edge path below.

DEFERRED, on purpose (see the PR body and CTO-200):

* The LATE-EDGE RESTITCH path (:func:`tally.stitcher.restitch_on_new_edge`): re-stitching an event
  that was unattributable on the first pass once a late identity edge reveals the converting user was
  someone we already had a touch for. It needs the identity graph loaded and a store of the pending
  unattributed events, and it only matters once confidence stitching exists. This job does a full
  from-scratch pass every tick instead, which is correct but does not retroactively rescue an event
  the moment an edge lands. Follow-up ticket.
* A per-tenant config TABLE for the rule (model + lookback). v1 uses a sensible default that works
  with no config row, which is the priority. If it grows, migration 0029 is next free.

BOUNDING THE CLICKHOUSE WORK (the CTO-219 posture, applied up front rather than learned the hard way).
Both loads are tenant-scoped, restricted to the lookback window, capped in rows, and run under
server-side ``max_rows_to_read`` / ``max_memory_usage`` / ``max_execution_time`` ceilings that make a
runaway scan FAIL rather than take ClickHouse (and with it the gateway) down. A failure raises, the
scheduler records ``failed`` and its backoff applies, and nothing wrong is ever written. The touch
store is loaded ONCE per run into memory and answers every ``query_last_touch`` from there, rather
than issuing a query per (event, rule) pair, which for a tenant with thousands of events and dozens of
feature tags would be tens of thousands of round trips an hour. It is still a ClickHouse-backed store:
one bounded scan sources it, and it implements exactly the protocol the stitcher calls.

HONESTY AND SAFETY. A tenant with no touches or no value events is a no-op that raises
:class:`~gateway.scheduler.JobSkipped` (settles the cadence window without claiming a run happened),
never an error. An unattributable value event is written to ``unattributed_events`` (queryable, never
silent) and is never guessed onto a random trace. Re-running does not duplicate: ``attribution_records``
is a ``ReplacingMergeTree`` whose sort key is ``(TenantId, BusinessEventId, FeatureTag)`` and whose
version is ``StitchedAt``, so a second pass replaces rather than appends, and the ``/features`` read
uses ``FINAL``. We rely on that and invent no second dedupe.

CADENCE: hourly, matching the reconciler (CTO-216). The stitcher reads our own ClickHouse rather than
anyone else's rate-limited API, and what it feeds is the ``/features`` economics that a human reads as
"current". An hour keeps that within a window a human would call fresh without re-deriving a number
that has barely moved every few minutes.

RUNS OFF THE EVENT LOOP. The scheduler calls job bodies on a worker thread, so :meth:`StitcherJob.__call__`
is plain synchronous blocking code. It owns its own :class:`gateway.store.ClickHouseStore` per run
(like the cost connector job, and unlike the ingest workers) so a job on a worker thread never shares a
``clickhouse-connect`` client with the request path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal

from tally.stitcher import (
    AttributionRecord,
    AttributionRule,
    BusinessEvent,
    Stitcher,
    Touch,
    UnattributedRecord,
    ValueType,
)

from gateway.config import Settings
from gateway.scheduler import Job, JobRegistry, JobSkipped
from gateway.store import ClickHouseStore

logger = logging.getLogger("tally.gateway.stitcher_job")

#: Persisted in ``scheduler_runs.job_name``, so it is a stable contract: renaming it orphans the
#: history and every tenant would look like it had never run, triggering an immediate run for all.
JOB_NAME = "stitcher"

#: Hourly. See the module docstring for why not finer and not daily.
HOURLY_INTERVAL_S = 3600.0

#: The default attribution rule the slice runs with when a tenant has no config row (which is every
#: tenant, since the config table is a follow-up). 30 days is the stitcher library's own default and
#: the window the ``/features`` economics is documented against.
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_ATTRIBUTION_MODEL = "last_touch_v1"

#: Stamped on every record so a future model change is distinguishable in the history.
STITCHER_VERSION = "v1"

#: Row caps on the two bounded loads. A tenant with more touches or events than this in the window
#: gets the newest, which is what last-touch attribution wants anyway; the ceilings below are the
#: hard stop that turns a runaway into a failed run rather than an OOM.
DEFAULT_TOUCH_CAP = 1_000_000
DEFAULT_EVENT_CAP = 200_000

#: Server-side ClickHouse ceilings (CTO-219). ``max_rows_to_read`` with ``read_overflow_mode=throw``
#: bounds rows SCANNED, which a ``LIMIT`` does not; the other two bound memory and wall time. A pass
#: that trips any of them raises, which the scheduler records as ``failed`` and backs off, rather than
#: taking the ClickHouse server down for every tenant after it.
DEFAULT_MAX_ROWS_TO_READ = 200_000_000
DEFAULT_MAX_MEMORY_BYTES = 2 * 1024**3  # 2 GiB
DEFAULT_MAX_EXECUTION_S = 120

#: Enum names permitted by the ``unattributed_events.Reason`` column in attribution.sql. The stitcher
#: emits ``no_trace_in_window``; anything unexpected is coerced to it rather than failing the insert.
_UNATTRIBUTED_REASONS: frozenset[str] = frozenset(
    {"no_trace_in_window", "unknown_user", "identity_unresolved", "feature_tag_missing"}
)


def _query_settings(
    *,
    max_rows_to_read: int = DEFAULT_MAX_ROWS_TO_READ,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    max_execution_s: int = DEFAULT_MAX_EXECUTION_S,
) -> dict[str, object]:
    """The CTO-219 ceilings, as a fresh dict per caller so nobody mutates a shared one."""
    return {
        "max_rows_to_read": int(max_rows_to_read),
        "read_overflow_mode": "throw",
        "max_memory_usage": int(max_memory_bytes),
        "max_execution_time": int(max_execution_s),
        "timeout_overflow_mode": "throw",
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """ClickHouse hands back naive UTC for a ``DateTime64`` with no timezone. Make that explicit,
    so the comparisons the stitcher does against ``event.occurred_at`` never raise a naive/aware
    ``TypeError``."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _hash_str(value: object) -> str:
    """A ``FixedString(64)`` column (``UserIdHash``) comes back from clickhouse-connect as ``bytes``,
    and ``str(bytes)`` would yield the ``b'...'`` repr rather than the hex, which then fails to bind
    back into a ``FixedString(64)`` and, worse, would not match a touch's hash. Decode it as ASCII
    and strip any trailing NUL padding so both sides of the attribution join are the same 64-char
    string they were written as."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("ascii", "replace").rstrip("\x00")
    return str(value)


def _dollars_to_micros(value: object) -> int:
    """``last_touch_index.LastTraceCost`` is a ``Decimal64(8)`` in dollars; the stitcher's
    :class:`~tally.stitcher.Touch` carries integer micro-USD. Convert once, at the boundary."""
    return int(round(float(value) * 1_000_000))


def _micros_to_dollars(micros: int) -> Decimal:
    """Inverse of :func:`_dollars_to_micros` for the ``AttributedTraceCost`` ``Decimal64(8)`` column.
    A Decimal (not a float) so the value binds to the decimal column without a binary-float wobble."""
    return Decimal(int(micros)) / Decimal(1_000_000)


def _value_type(raw: object) -> ValueType:
    """Map the ``business_events.ValueType`` enum name back to the stitcher's :class:`ValueType`.
    An unrecognised value falls back to ``monetary`` rather than failing the whole load."""
    try:
        return ValueType(str(raw))
    except ValueError:
        return ValueType.MONETARY


class ClickHouseTouchStore:
    """A :class:`tally.stitcher.TouchStore` sourced by ONE bounded load of ``last_touch_index``.

    WHY bulk-load rather than a query per call. The stitcher calls ``query_last_touch`` once per
    ``(event, rule)`` pair. A tenant with thousands of value events and dozens of feature tags is tens
    of thousands of pairs, and one ClickHouse round trip each would be the most expensive thing the
    gateway does every hour. So a single capped, memory- and time-bounded scan (the CTO-219 posture)
    loads the newest touch per ``(UserIdHash, FeatureTag)`` in the window into memory, and every
    ``query_last_touch`` is answered from there. It still IS a ClickHouse-backed store: ClickHouse
    sources it, and it implements exactly the protocol the stitcher expects.

    The load window is wider than the value-event window by one lookback: an event at the very start
    of ``[now - lookback, now]`` still has to be able to see a touch a full lookback before IT, so the
    touches are loaded over ``[now - 2*lookback, now]``. The precise per-event window is then enforced
    in memory by :meth:`query_last_touch`, which the stitcher calls with ``[occurred - lookback,
    occurred]``.

    ``ReplacingMergeTree`` on ``(TenantId, UserIdHash, FeatureTag)`` means multiple physical rows can
    exist per key before a merge, so the load collapses them with ``argMax(..., UpdatedAt)`` rather
    than trusting the parts to be merged.
    """

    _SQL = """
        SELECT UserIdHash,
               FeatureTag,
               argMax(LastTraceId, UpdatedAt)           AS trace_id,
               argMax(LastTraceTs, UpdatedAt)           AS trace_ts,
               argMax(LastTraceCost, UpdatedAt)         AS trace_cost,
               argMax(UserIdHashKeyVersion, UpdatedAt)  AS key_version
        FROM last_touch_index
        WHERE TenantId = {tenant:String}
          AND LastTraceTs >= subtractDays(now64(9), {days:UInt32})
          AND LastTraceTs <= now64(9)
        GROUP BY UserIdHash, FeatureTag
        ORDER BY trace_ts DESC
        LIMIT {cap:UInt32}
    """

    def __init__(
        self,
        store: object,
        *,
        touch_lookback_days: int,
        cap: int = DEFAULT_TOUCH_CAP,
        query_settings: dict[str, object] | None = None,
    ) -> None:
        if touch_lookback_days <= 0 or cap <= 0:
            raise ValueError("touch_lookback_days and cap must be positive")
        self._store = store
        self._days = int(touch_lookback_days)
        self._cap = int(cap)
        self._settings = query_settings if query_settings is not None else _query_settings()
        self._by_key: dict[tuple[str, str], Touch] = {}

    @property
    def query_settings(self) -> dict[str, object]:
        """The ClickHouse-side ceilings this store enforces. Exposed so tests can assert on them."""
        return dict(self._settings)

    @property
    def touch_count(self) -> int:
        return len(self._by_key)

    def feature_tags(self) -> set[str]:
        """Every feature tag seen in the loaded window. Drives the default rule set, so it costs no
        extra query."""
        return {feature_tag for (_user, feature_tag) in self._by_key}

    def load(self, tenant_id: str) -> int:
        """Run the one bounded scan and index the result. Returns how many touches were loaded.

        Raises on a ClickHouse failure (including a tripped ceiling), which the job turns into a
        ``failed`` run: the honest outcome, never a partial index that looks complete.
        """
        result = self._store.client.query(  # type: ignore[attr-defined]
            self._SQL,
            parameters={"tenant": tenant_id, "days": self._days, "cap": self._cap},
            settings=self._settings,
        )
        by_key: dict[tuple[str, str], Touch] = {}
        for row in result.result_rows:
            user_hash, feature_tag, trace_id, trace_ts, trace_cost, key_version = row
            touch = Touch(
                trace_id=str(trace_id),
                user_hash=_hash_str(user_hash),
                feature_tag=str(feature_tag),
                ts=_as_utc(trace_ts),
                cost_micro_usd=_dollars_to_micros(trace_cost),
                key_version=str(key_version) or "v1",
            )
            key = (touch.user_hash, touch.feature_tag)
            current = by_key.get(key)
            # argMax already collapses the ReplacingMergeTree per key, so this only guards the
            # theoretical case of two rows surviving; newest LastTraceTs wins, as last-touch wants.
            if current is None or touch.ts > current.ts:
                by_key[key] = touch
        self._by_key = by_key
        return len(by_key)

    def query_last_touch(
        self,
        *,
        tenant_id: str,
        user_hashes: set[str],
        feature_tag: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Touch | None:
        """The most recent loaded touch for any of ``user_hashes`` on ``feature_tag`` within the
        window, or ``None``. Same shape and semantics as :class:`tally.stitcher.MemoryTouchStore`, so
        the stitcher cannot tell the two apart. ``tenant_id`` is already fixed by :meth:`load`; it is
        accepted to satisfy the protocol and is not re-checked."""
        best: Touch | None = None
        for user_hash in user_hashes:
            touch = self._by_key.get((user_hash, feature_tag))
            if touch is None:
                continue
            if touch.ts < window_start or touch.ts > window_end:
                continue
            if best is None or touch.ts > best.ts:
                best = touch
        return best


class ClickHouseValueEventSource:
    """Loads a tenant's recent value events from ``business_events`` as :class:`BusinessEvent`s.

    ``FINAL`` collapses the ``ReplacingMergeTree`` so a re-posted event (same ``BusinessEventId``) is
    read once, not twice, which would otherwise attribute the same conversion twice. Bounded exactly
    like the touch load: tenant-scoped, windowed, capped and under the CTO-219 ceilings. Events with
    an empty ``UserIdHash`` are skipped in SQL, because there is nothing to resolve them to and they
    would only ever be unattributable.
    """

    _SQL = """
        SELECT BusinessEventId,
               EventName,
               UserIdHash,
               OccurredAt,
               ValueAmountMicro,
               ValueCurrency,
               ValueType
        FROM business_events FINAL
        WHERE TenantId = {tenant:String}
          AND UserIdHash != ''
          AND OccurredAt >= subtractDays(now64(9), {days:UInt32})
          AND OccurredAt <= now64(9)
        ORDER BY OccurredAt DESC
        LIMIT {cap:UInt32}
    """

    def __init__(
        self,
        *,
        lookback_days: int,
        cap: int = DEFAULT_EVENT_CAP,
        query_settings: dict[str, object] | None = None,
    ) -> None:
        if lookback_days <= 0 or cap <= 0:
            raise ValueError("lookback_days and cap must be positive")
        self._days = int(lookback_days)
        self._cap = int(cap)
        self._settings = query_settings if query_settings is not None else _query_settings()

    @property
    def query_settings(self) -> dict[str, object]:
        return dict(self._settings)

    def fetch(self, store: object, tenant_id: str) -> list[BusinessEvent]:
        """One bounded, tenant-scoped round trip. Raises on a ClickHouse failure (see the touch load)."""
        result = store.client.query(  # type: ignore[attr-defined]
            self._SQL,
            parameters={"tenant": tenant_id, "days": self._days, "cap": self._cap},
            settings=self._settings,
        )
        events: list[BusinessEvent] = []
        for row in result.result_rows:
            business_event_id, event_name, user_hash, occurred_at, value_micro, currency, vtype = row
            events.append(
                BusinessEvent(
                    business_event_id=str(business_event_id),
                    tenant_id=tenant_id,
                    user_hash=_hash_str(user_hash),
                    event_name=str(event_name),
                    occurred_at=_as_utc(occurred_at),
                    value_amount_micro=(int(value_micro) if value_micro is not None else None),
                    value_currency=str(currency) or "USD",
                    value_type=_value_type(vtype),
                )
            )
        return events


#: Column order for ``attribution_records``, pinned against the DDL in attribution.sql. A column added
#: to one and not the other silently shifts every value after it, so the tests pin this list to the
#: canonical DDL, exactly as ``store._BUSINESS_EVENT_COLS`` is pinned.
_ATTRIBUTION_COLS = (
    "TenantId",
    "BusinessEventId",
    "FeatureTag",
    "AttributedTraceId",
    "AttributedTraceTs",
    "AttributedTraceCost",
    "ValueAmountMicro",
    "ValueCurrency",
    "AttributionModel",
    "AttributionConfidence",
    "UserIdHashKeyVersion",
    "LookbackWindowDays",
    "StitchedAt",
    "StitcherVersion",
)

#: Column order for ``unattributed_events``, pinned the same way.
_UNATTRIBUTED_COLS = (
    "TenantId",
    "BusinessEventId",
    "EventName",
    "UserIdHash",
    "OccurredAt",
    "Reason",
    "LastCheckedAt",
)


class ClickHouseAttributionWriter:
    """Writes the stitcher's output back to ClickHouse: records to ``attribution_records``, the
    unattributable events to ``unattributed_events``.

    No dedupe of its own. ``attribution_records`` is a ``ReplacingMergeTree`` keyed on
    ``(TenantId, BusinessEventId, FeatureTag)`` with ``StitchedAt`` as the version, so re-running the
    job replaces rather than appends and the ``/features`` read collapses with ``FINAL``. Inventing a
    second dedupe here would only be a way to disagree with that one.
    """

    def write(
        self,
        store: object,
        records: Sequence[AttributionRecord],
        unattributed: Sequence[UnattributedRecord],
    ) -> tuple[int, int]:
        """Insert both sets. Returns ``(records_written, unattributed_written)``."""
        if records:
            rows = [
                (
                    record.tenant_id,
                    record.business_event_id,
                    record.feature_tag,
                    record.attributed_trace_id,
                    record.attributed_trace_ts,
                    _micros_to_dollars(record.attributed_trace_cost_micro_usd),
                    record.value_amount_micro,
                    record.value_currency,
                    record.attribution_model,
                    # str value of the enum, which is the ClickHouse Enum8 NAME ('direct', ...).
                    record.confidence.value,
                    record.user_id_hash_key_version,
                    record.lookback_window_days,
                    record.stitched_at,
                    record.stitcher_version,
                )
                for record in records
            ]
            store.client.insert(  # type: ignore[attr-defined]
                "attribution_records", rows, column_names=list(_ATTRIBUTION_COLS)
            )
        if unattributed:
            urows = [
                (
                    u.tenant_id,
                    u.business_event_id,
                    u.event_name,
                    u.user_hash,
                    u.occurred_at,
                    u.reason if u.reason in _UNATTRIBUTED_REASONS else "no_trace_in_window",
                    u.last_checked_at,
                )
                for u in unattributed
            ]
            store.client.insert(  # type: ignore[attr-defined]
                "unattributed_events", urows, column_names=list(_UNATTRIBUTED_COLS)
            )
        return len(records), len(unattributed)


def _default_rules(
    event_names: set[str], feature_tags: set[str], *, lookback_days: int
) -> list[AttributionRule]:
    """The default rule set: one rule per ``(event_name, feature_tag)`` actually present for the
    tenant, at the default window and model. See the module docstring for why last-touch attribution
    is exactly this product. Sorted so a run is deterministic and reproducible in tests."""
    return [
        AttributionRule(
            event_name=event_name,
            feature_tag=feature_tag,
            lookback_days=lookback_days,
            attribution_model=DEFAULT_ATTRIBUTION_MODEL,
        )
        for event_name in sorted(event_names)
        for feature_tag in sorted(feature_tags)
    ]


class StitcherJob:
    """Stitches a tenant's value events against its touches and writes the result. Callable as a job.

    Constructed once at startup and called with a tenant id per due tick. Holds no per-tenant state:
    everything is re-read on every call, so a tenant that starts converting today attributes on the
    next tick with no restart.

    Every collaborator is injectable so the tests never touch ClickHouse. The defaults are the
    production wiring.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store_factory: Callable[[], ClickHouseStore] | None = None,
        writer: ClickHouseAttributionWriter | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        touch_cap: int = DEFAULT_TOUCH_CAP,
        event_cap: int = DEFAULT_EVENT_CAP,
        query_settings: dict[str, object] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        self._settings = settings
        # Own client per run, like the cost connector job: a job on a worker thread must never share a
        # clickhouse-connect client with the request path.
        self._store_factory = (
            store_factory if store_factory is not None else lambda: ClickHouseStore(settings)
        )
        self._writer = writer if writer is not None else ClickHouseAttributionWriter()
        self._lookback_days = int(lookback_days)
        self._touch_cap = int(touch_cap)
        self._event_cap = int(event_cap)
        self._query_settings = query_settings
        self._now = now

    def __call__(self, tenant_id: str) -> None:
        """Run one full stitch pass for ``tenant_id``. Blocking by design (worker thread).

        Skips (``JobSkipped``) when the tenant has no touches or no value events, which is a no-op that
        settles the cadence window without claiming a run happened. Otherwise stitches every event
        against the default rules, writes the records and the unattributable events, and returns.
        """
        store = self._store_factory()
        try:
            touches = ClickHouseTouchStore(
                store,
                # Load one lookback WIDER than the events, so an event at the start of the event
                # window can still see a touch a full lookback before it. See the class docstring.
                touch_lookback_days=self._lookback_days * 2,
                cap=self._touch_cap,
                query_settings=(
                    dict(self._query_settings) if self._query_settings is not None else None
                ),
            )
            touches.load(tenant_id)

            event_source = ClickHouseValueEventSource(
                lookback_days=self._lookback_days,
                cap=self._event_cap,
                query_settings=(
                    dict(self._query_settings) if self._query_settings is not None else None
                ),
            )
            events = event_source.fetch(store, tenant_id)

            feature_tags = touches.feature_tags()
            # No touches or no events is the normal state for most tenants and is a no-op, not an
            # error. Nothing is written: with no touches nothing could attribute, and with no events
            # there is nothing to attribute. See HONESTY AND SAFETY in the module docstring.
            if not feature_tags or not events:
                raise JobSkipped(
                    f"tenant {tenant_id} has "
                    f"{'no touches' if not feature_tags else 'no value events'}"
                )

            event_names = {event.event_name for event in events}
            rules = _default_rules(
                event_names, feature_tags, lookback_days=self._lookback_days
            )

            # Empty identity graph: v1 attributes on a direct user-hash match only. See CONFIDENCE in
            # the module docstring. now= anchors the stitch clock; each rule's own window is measured
            # back from the event's occurred_at inside stitch().
            stitcher = Stitcher(touches=touches, rules=rules)
            now = self._now()
            for event in events:
                stitcher.stitch(event, now=now)

            records = list(stitcher.records.values())
            attributed_ids = {record.business_event_id for record in records}
            # Only events that attributed to NOTHING are written as unattributed. The stitcher keys
            # its unattributed set on (tenant, event) without the feature tag, so an event that
            # attributed to one feature but not another can leave a stale unattributed entry depending
            # on rule order; filtering on "produced zero records" is the honest, order-independent
            # definition of unattributable and avoids writing a row that contradicts an attribution.
            unattributed = [
                record
                for (_tenant, business_event_id), record in stitcher.unattributed.items()
                if business_event_id not in attributed_ids
            ]

            written, unattributed_written = self._writer.write(store, records, unattributed)
            logger.info(
                "stitcher: tenant=%s events=%d touches=%d rules=%d records=%d unattributed=%d",
                tenant_id,
                len(events),
                touches.touch_count,
                len(rules),
                written,
                unattributed_written,
            )
        finally:
            # Closed on every path, including the JobSkipped raise. A leaked ClickHouse client per run
            # would take a long time to notice and would look like a ClickHouse problem.
            store.close()


def register_stitcher_job(
    registry: JobRegistry,
    settings: Settings,
    *,
    interval_s: float = HOURLY_INTERVAL_S,
    job: StitcherJob | None = None,
) -> Job:
    """Register the hourly stitcher job on ``registry`` (called from the gateway lifespan).

    Kept here rather than inside ``build_scheduler`` so the scheduler core stays a job-free engine and
    each job's wiring lives next to the job, exactly as :func:`register_cost_connector_job` and
    :func:`register_worker_jobs` do.
    """
    return registry.register(
        JOB_NAME,
        interval_s,
        job if job is not None else StitcherJob(settings),
    )


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "HOURLY_INTERVAL_S",
    "JOB_NAME",
    "STITCHER_VERSION",
    "ClickHouseAttributionWriter",
    "ClickHouseTouchStore",
    "ClickHouseValueEventSource",
    "StitcherJob",
    "register_stitcher_job",
]
