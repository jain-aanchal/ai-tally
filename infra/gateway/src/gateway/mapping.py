"""Map an enriched span attribute dict onto an ``otel_spans`` ClickHouse row.

Pure functions (no infra) so the translation is unit-testable. The SDK emits ``gen_ai.*`` attribute
dicts (see :func:`tally.schema.build_span_attributes`); a span may additionally carry structural
keys (``TraceId``/``trace_id``, ``SpanId``, ``Timestamp``, ``ServiceName``, ...). High-value
attributes are promoted to typed columns; everything else lands in the ``SpanAttributes`` map.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import blake2b

from tally.schema import BILLING_MODE_SUBSCRIPTION, GenAI, micro_to_usd
from tally.timekeeping import representable_ts_ns

# gen_ai.* keys that get promoted to typed columns (so they don't also duplicate into the map).
_PROMOTED_GENAI = frozenset(
    {
        GenAI.FEATURE_TAG,
        GenAI.SESSION_ID,
        GenAI.USER_ID_HASH,
        GenAI.USER_ID_HASH_KEY_VERSION,
        GenAI.ACCOUNT_ID_HASH,
        GenAI.ACCOUNT_ID_HASH_KEY_VERSION,
        GenAI.IDEMPOTENCY_KEY,
        GenAI.SYSTEM,
        GenAI.REQUEST_MODEL,
        GenAI.RESPONSE_MODEL,
        GenAI.OPERATION_NAME,
        GenAI.TOOL_NAME,
        GenAI.USAGE_INPUT_TOKENS,
        GenAI.USAGE_OUTPUT_TOKENS,
        GenAI.USAGE_CACHED_INPUT_TOKENS,
        GenAI.COST_ESTIMATED_MICRO_USD,
        GenAI.COST_CURRENCY,
        GenAI.COST_PRICE_CATALOG_VERSION,
        GenAI.AGENT_RUN_ID,
        GenAI.AGENT_STEP_INDEX,
        GenAI.RESOLVED_CONTEXT_REF,
        GenAI.CONTEXT_DROPPED_MESSAGES,
        GenAI.CONTEXT_DROPPED_TOKENS,
        GenAI.CONTEXT_WINDOW_USED_PCT,
        GenAI.SAMPLING_STRATUM,
        GenAI.SAMPLING_RATE,
    }
)

# Wire-only gen_ai.* keys: accepted on ingest, deliberately NOT written to ClickHouse: neither as
# a typed column nor as a SpanAttributes entry. They are listed here so the long-tail loop below
# drops them explicitly rather than letting them fall through into the map by accident.
#
# gen_ai.account_label (CTO-181) is the only member today. An account label is mutable,
# human-readable customer metadata: stamping it on every span would put customer names in the
# telemetry store, which is exactly what hashing the account id exists to prevent (see the comment
# on otel_spans.AccountIdHash in db/clickhouse/otel_spans.sql). Labels belong in the Postgres
# control plane, keyed on the account hash and joined at render time.
#
# SEAM FOR CTO-186 (B7): the label store does not exist yet. When it lands, the upsert hangs off
# :func:`wire_only_account_label` below. The value is already parsed out of the span here, so B7
# adds a store call at the ingest site and nothing in this module has to change.
_WIRE_ONLY_GENAI = frozenset({GenAI.ACCOUNT_LABEL})


def wire_only_account_label(span: dict[str, object]) -> str | None:
    """Return the wire-only ``gen_ai.account_label`` for this span, or None when absent.

    This is the seam CTO-186 (B7) hangs the Postgres label-store upsert on. Today nothing calls it
    on the write path: the label is accepted, validated, and dropped. It is never persisted to
    ClickHouse and never appears in a row tuple or in ``SpanAttributes``.
    """
    v = span.get(GenAI.ACCOUNT_LABEL)
    return v if isinstance(v, str) and v else None


# Hard PII guard (CTO-118): any incoming attribute whose key tail-segment matches one of
# these is dropped on the floor. The contract is "counts only, never bodies"; if a caller
# tries to sneak a message body through under a familiar name, we refuse to persist it.
# Match by suffix so nested namespaces (e.g. "gen_ai.prompt.text") are caught too.
_BODY_KEY_SUFFIXES = frozenset(
    {
        "message_text",
        "messages",
        "prompt",
        "prompt_text",
        "completion",
        "completion_text",
        "input_text",
        "output_text",
        "content",
        "text",
        "body",
    }
)


def _is_body_key(key: str) -> bool:
    """Return True if the key's last dot-segment looks like it could carry a message body."""
    tail = key.rsplit(".", 1)[-1].lower()
    return tail in _BODY_KEY_SUFFIXES

# Structural keys recognised on the raw span dict (snake_case or ClickHouse-case both accepted).
_STRUCTURAL = frozenset(
    {
        "TraceId", "trace_id", "SpanId", "span_id", "ParentSpanId", "parent_span_id",
        "Timestamp", "timestamp_ns", "ServiceName", "service_name", "SpanName", "span_name",
        "StatusCode", "status_code", "DurationNs", "duration_ns",
    }
)

# Ordered column list for the ClickHouse insert. Must match the row tuples produced below.
COLUMNS: tuple[str, ...] = (
    "TenantId",
    "Timestamp",
    "TraceId",
    "SpanId",
    "ParentSpanId",
    "ServiceName",
    "SpanName",
    "StatusCode",
    "DurationNs",
    "FeatureTag",
    "SessionId",
    "UserIdHash",
    "UserIdHashKeyVersion",
    # Account dimension (CTO-180/182). Placed to mirror the DDL, immediately after the user hash.
    "AccountIdHash",
    "AccountIdHashKeyVersion",
    "IdempotencyKey",
    "GenAiSystem",
    "GenAiRequestModel",
    "GenAiResponseModel",
    "GenAiOperation",
    "GenAiToolName",
    "InputTokens",
    "OutputTokens",
    "CachedInputTokens",
    "EstimatedCost",
    "CostCurrency",
    "CostSource",
    "PriceCatalogVersion",
    "AgentRunId",
    "AgentStepIndex",
    "ContextDroppedMessages",
    "ContextDroppedTokens",
    "ContextWindowUsedPct",
    "SamplingStratum",
    "SamplingRate",
    "SpanAttributes",
    "SampleRate",
)


def _pick(span: dict[str, object], *keys: str) -> object | None:
    for k in keys:
        if k in span and span[k] is not None:
            return span[k]
    return None


def _s(v: object | None) -> str:
    return "" if v is None else str(v)


def _i(v: object | None) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _i_or_none(v: object | None) -> int | None:
    """Like :func:`_i` but returns None (-> ClickHouse NULL) when the value is not a number.

    CTO-244. The columns this feeds (InputTokens/OutputTokens/CachedInputTokens) are Nullable
    precisely so that "the provider never told us" is representable. Coercing an absent or
    unparseable count to 0 asserted that a real, billed call consumed nothing, which is a
    fabricated number, not a conservative one. A provider-reported 0 is a number and still
    stores as 0, distinct from NULL.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _fixed64(v: object | None) -> str:
    """FixedString(64) wants exactly 64 bytes; ClickHouse pads, but truncate over-long input."""
    s = _s(v)
    return s[:64]


def _f(v: object | None) -> float:
    """Coerce to float, defaulting to 0.0, clamped to [0, 1] (we only promote a fraction)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    f = float(v)
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


# CTO-402: personalisation strings that domain-separate the two derived ids. The same material is
# hashed twice, so these keep the trace and span digests independent rather than related values.
# Precision about how much work they are actually doing, because an earlier version of this comment
# overclaimed: blake2b folds digest_size into its parameter block, so a 16-byte and an 8-byte digest
# of identical material already differ completely and neither is a truncation of the other. The
# personalisation is therefore belt-and-braces, not the sole thing preventing a shared digest. It is
# kept because it costs nothing and it makes the domain separation explicit instead of resting on an
# implementation detail of the hash. Changing either string changes every derived id, which the
# golden-vector test in tests/test_idless_span_identity.py pins.
_TRACE_ID_PERSON = b"tally-trace"
_SPAN_ID_PERSON = b"tally-span"


def _derive_span_ids(
    span: dict[str, object],
    *,
    tenant_id: str,
    effective_ts_ns: int,
    batch_index: int,
) -> tuple[str, str]:
    """Derive a DETERMINISTIC ``(trace_id, span_id)`` for a span that arrived without them (CTO-402).

    These used to be fresh random uuids minted at row-build time, which meant two writes of the same
    logical span got different sorting-key values and the ReplacingMergeTree backstop
    (db/clickhouse/otel_spans.sql, ORDER BY (..., Timestamp, TraceId, SpanId)) could never collapse
    them. Since CTO-396 stopped ``deduplicated()`` collapsing id-less spans, an id-less producer
    retrying a batch under a new batch id wrote 500 + 500 rows where it previously wrote 1 + 1, and
    the rollup materialized views fire on INSERT, so the duplicate is permanent in the rollups.
    Deriving the id from content gives the backstop an identity to collapse on without
    re-introducing the intra-batch collapse that CTO-396 fixed.

    WHICH DICT IS CANONICAL: ``span`` MUST be the span exactly as the producer POSTED it, before
    cost enrichment. The caller passes it explicitly (``span_to_row(identity_span=...)``); it is not
    the enriched dict the row is built from. Hashing the enriched attributes was the original
    mistake: ``enrich_cost`` stamps ``gen_ai.cost.price_catalog_version`` and a server-recomputed
    ``gen_ai.cost.estimated_micro_usd`` onto its copy, so a catalog reload or a rolling deploy
    between an attempt and its retry moved the derived id and the rows stopped collapsing, which
    defeats the entire fix. The posted span is the only thing a retry reproduces byte for byte.

    A consequence of that choice, stated rather than left to be found: keys the mapper DROPS still
    influence the id. A body-shaped key (:func:`_is_body_key`) and a wire-only key
    (``_WIRE_ONLY_GENAI``, i.e. ``gen_ai.account_label``) are never written to ClickHouse, but they
    are in the posted span and therefore in this digest. Two rows that are byte-identical in storage
    can thus carry different derived ids if they were posted with different account labels. That
    direction is the safe one: it separates spans that would otherwise share an id, and it never
    collapses two spans that genuinely differ.

    WHAT GOES INTO THE HASH, and why each part has to:

    * ``tenant_id``: two tenants' spans can never share a derived id, so one tenant's traffic can
      never collapse another's. The sorting key starts with TenantId anyway, but an id that repeats
      across tenants would still be wrong on every other read path.
    * ``effective_ts_ns``: distinct moments stay distinct, so a genuinely repeated call (the same
      span content, later) is a different span rather than a collapsed one. It also keeps a derived
      id from repeating across billing periods.
    * ``batch_index``: the span's position in the batch as posted. This is what separates 500
      genuinely distinct-but-identical spans in one batch, which is exactly the traffic CTO-396
      stopped discarding. A retry of the same batch presents the same spans in the same order and
      so reproduces the same ids; the batch id is deliberately NOT in the material, because it is
      the thing that differs between a first attempt and its retry.
    * the span's own attributes as posted: two spans that differ in any attribute get different ids.

    KNOWN LIMITATION, AND IT DELETES REAL SPEND. This derivation cannot tell two genuinely distinct
    spans apart when they agree on every input above: same tenant, identical posted content,
    identical ``effective_ts_ns``, and the same ``batch_index``. They derive the same ids, so they
    derive the same sorting key, and a ReplacingMergeTree merge keeps ONE of them and deletes the
    other permanently, along with its cost. The realistic trigger is not exotic: a coarse clock
    (``Date.now() * 1e6`` gives millisecond resolution), two replicas issuing the same repeated call
    (a cache warm, a health check, an embedding of a fixed string), and single-span flushes, which
    put every span at ``batch_index`` 0. This is information-theoretically unavoidable here: without
    a producer-supplied id there is nothing left to distinguish two spans that are identical in
    every observable respect, and the alternative (never collapsing) is the duplicate-spend bug this
    fix exists to close. It is a deliberate trade, not an oversight, and it is pinned by
    ``test_known_collapse_identical_spans_at_the_same_instant_and_index`` so nobody "fixes" it by
    accident. It is limitation 4 in db/clickhouse/otel_spans.sql. The real fix remains a producer
    that sends its own ids, or the durable idempotency store.

    NO CUSTOMER DATA IS EXPOSED. The material is hashed and never stored, and a blake2b digest is
    not reversible back to the attributes that produced it. Identifiers in it are HMAC hashes by the
    time they reach here (CTO-118/182). Note the honest caveat to the older wording: it is NOT true
    that every attribute in the material is also stored on the row, precisely because this hashes
    the posted span (see above), and the body-key filter lives in this module (:func:`_is_body_key`)
    rather than in the validator.
    """
    material = json.dumps(
        {
            "tenant_id": tenant_id,
            "effective_ts_ns": effective_ts_ns,
            "batch_index": batch_index,
            "span": span,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    # Widths match what the random fallback produced: 32 hex for a trace, 16 for a span.
    trace_id = blake2b(material, digest_size=16, person=_TRACE_ID_PERSON).hexdigest()
    span_id = blake2b(material, digest_size=8, person=_SPAN_ID_PERSON).hexdigest()
    return trace_id, span_id


def span_to_row(
    span: dict[str, object],
    *,
    tenant_id: str,
    effective_ts_ns: int,
    sample_rate: float = 1.0,
    batch_index: int = 0,
    identity_span: dict[str, object] | None = None,
) -> tuple[object, ...]:
    """Translate one enriched span attribute dict into an ``otel_spans`` row tuple.

    ``effective_ts_ns`` is the skew-clamped timestamp from :func:`tally.timekeeping.assess`. Cost is
    converted from integer micro-USD back to a :class:`~decimal.Decimal` for the Decimal64(8) column.

    ``batch_index`` is the span's position in the batch as posted, and only matters for a span that
    arrived without ids: it is part of the material their deterministic replacement is derived from
    (CTO-402, see :func:`_derive_span_ids`). A caller mapping a single span outside a batch can
    leave it at its default.

    ``identity_span`` is the span as the producer POSTED it, before cost enrichment, and is what an
    id-less span's deterministic id is hashed from (CTO-402). It matters because ``span`` here is
    the ENRICHED dict: it carries a server-recomputed cost and ``gen_ai.cost.price_catalog_version``,
    both of which move when the price catalog reloads, so hashing it would make a retry derive a
    different id and stop collapsing. The ingest path passes the raw span; a caller that has only
    one dict can leave this None and the enriched ``span`` is used, which is the pre-existing
    behaviour and is correct for callers that supply their own ids anyway
    (gateway/connectors/base.py).
    """
    ts = datetime.fromtimestamp(representable_ts_ns(effective_ts_ns) / 1e9, tz=timezone.utc)

    # CTO-244. A span that carries no priced cost is UNPRICED, not free. Writing Decimal(0) here
    # told every downstream sum that a real call cost nothing; the honest write is NULL plus the
    # reason. The reason reuses the existing cost-source notion rather than a parallel one:
    # CostSource = 'unpriced', alongside the empty PriceCatalogVersion that tally.pricing already
    # returns on a catalog miss (see compute_cost_micro_usd, which yields version "" when a rate is
    # missing). A provider/SDK-reported cost of exactly 0 micro-USD is a real priced zero and is
    # still stored as 0 with CostSource = 'estimated'.
    cost_micro = span.get(GenAI.COST_ESTIMATED_MICRO_USD)
    priced = isinstance(cost_micro, int) and not isinstance(cost_micro, bool)
    estimated_cost: Decimal | None = micro_to_usd(cost_micro) if priced else None
    cost_source = "estimated" if priced else "unpriced"

    # CTO-417. A subscription-billed call has no per-call price to know, which is a DIFFERENT fact
    # from 'unpriced' ("we could not put a number on this"), and a reader needs to tell them apart:
    # an unpriced span is a gap someone can close by seeding a rate, a subscription span never will
    # be. Hence the dedicated enum value rather than folding it into 'unpriced'.
    #
    # This repeats the veto that tally.enrichment.enrich_cost already applies, deliberately. That
    # one is the enrichment path's decision; this one is the storage boundary's, and it is the last
    # place a cost can be written. A caller that maps a span without going through enrichment
    # (gateway/connectors/base.py does exactly that) would otherwise stamp a cost on a span its
    # producer said was not billed per call. Neither guard relies on the other.
    #
    # The marker is CLIENT-ASSERTED. Nothing here can corroborate it; see tally.schema.BILLING_MODES
    # and CTO-410. It removes cost from the tenant's own spend picture, so it is safe for
    # observability and must not be used as a billing control.
    billing_mode = span.get(GenAI.COST_BILLING_MODE)
    if isinstance(billing_mode, str) and billing_mode.strip().lower() == BILLING_MODE_SUBSCRIPTION:
        estimated_cost = None
        cost_source = "subscription"

    # Long-tail attributes: anything not promoted and not structural, stringified for Map(String,String).
    # PII guard (CTO-118): refuse to persist any key that looks like it could carry a message body.
    extra: dict[str, str] = {}
    for k, v in span.items():
        if k in _PROMOTED_GENAI or k in _STRUCTURAL or v is None:
            continue
        # Wire-only keys (gen_ai.account_label) never reach ClickHouse, not even via the map.
        if k in _WIRE_ONLY_GENAI:
            continue
        if _is_body_key(str(k)):
            continue
        # CTO-401 review: str() is PYTHON's spelling, so a bool lands here as 'True'/'False', not
        # 'true'/'false'. gen_ai.trace_id_synthetic is the first bool-valued attribute to reach this
        # map, and a query written as SpanAttributes['gen_ai.trace_id_synthetic'] = 'true' matches
        # nothing and returns an empty result that looks exactly like "no synthetic spans". Left as
        # str() deliberately: normalising here would change the stored spelling of every long-tail
        # value for one key's benefit, and rows already written would keep the old spelling, so the
        # ambiguity would survive the fix. Documented, and pinned by a test, instead. Match 'True'.
        extra[str(k)] = str(v)

    # CTO-402: an id the producer sent is always kept; only a missing one is derived, and it is
    # derived rather than randomised so that re-writing the same logical span reproduces the same
    # sorting key and the ReplacingMergeTree backstop can collapse it.
    trace_id = _pick(span, "TraceId", "trace_id")
    span_id = _pick(span, "SpanId", "span_id")
    if not trace_id or not span_id:
        derived_trace_id, derived_span_id = _derive_span_ids(
            identity_span if identity_span is not None else span,
            tenant_id=tenant_id,
            effective_ts_ns=effective_ts_ns,
            batch_index=batch_index,
        )
        trace_id = trace_id or derived_trace_id
        span_id = span_id or derived_span_id

    return (
        tenant_id,
        ts,
        _s(trace_id),
        _s(span_id),
        _s(_pick(span, "ParentSpanId", "parent_span_id")),
        _s(_pick(span, "ServiceName", "service_name") or "unknown"),
        _s(_pick(span, "SpanName", "span_name") or span.get(GenAI.OPERATION_NAME) or "llm.call"),
        _i(_pick(span, "StatusCode", "status_code")),
        _i(_pick(span, "DurationNs", "duration_ns")),
        _s(span.get(GenAI.FEATURE_TAG) or "untagged"),
        _s(span.get(GenAI.SESSION_ID)),
        _fixed64(span.get(GenAI.USER_ID_HASH)),
        _s(span.get(GenAI.USER_ID_HASH_KEY_VERSION)),
        # CTO-182: an absent account hash writes '' (the UNATTRIBUTED bucket the DDL documents),
        # never a null and never a placeholder like 'unknown' that could rank as a real account.
        _fixed64(span.get(GenAI.ACCOUNT_ID_HASH)),
        _s(span.get(GenAI.ACCOUNT_ID_HASH_KEY_VERSION)),
        _s(span.get(GenAI.IDEMPOTENCY_KEY)),
        _s(span.get(GenAI.SYSTEM)),
        _s(span.get(GenAI.REQUEST_MODEL)),
        _s(span.get(GenAI.RESPONSE_MODEL)),
        _s(span.get(GenAI.OPERATION_NAME)),
        _s(span.get(GenAI.TOOL_NAME)),
        # CTO-244: absent usage writes NULL, never 0. See _i_or_none.
        _i_or_none(span.get(GenAI.USAGE_INPUT_TOKENS)),
        _i_or_none(span.get(GenAI.USAGE_OUTPUT_TOKENS)),
        _i_or_none(span.get(GenAI.USAGE_CACHED_INPUT_TOKENS)),
        estimated_cost,
        _s(span.get(GenAI.COST_CURRENCY) or "USD"),
        cost_source,
        _s(span.get(GenAI.COST_PRICE_CATALOG_VERSION)),
        _s(span.get(GenAI.AGENT_RUN_ID)),
        _i(span.get(GenAI.AGENT_STEP_INDEX)),
        _i(span.get(GenAI.CONTEXT_DROPPED_MESSAGES)),
        _i(span.get(GenAI.CONTEXT_DROPPED_TOKENS)),
        _f(span.get(GenAI.CONTEXT_WINDOW_USED_PCT)),
        # CTO-119: stratum default 'unsampled' matches the DDL; pre-CTO-119 spans land in their
        # own honestly-labelled bucket on the DQ surface rather than masquerading as 'body'.
        _s(span.get(GenAI.SAMPLING_STRATUM) or "unsampled"),
        _f(span.get(GenAI.SAMPLING_RATE) if span.get(GenAI.SAMPLING_RATE) is not None else 1.0),
        extra,
        float(sample_rate),
    )
