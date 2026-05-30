"""Map an enriched span attribute dict onto an ``otel_spans`` ClickHouse row.

Pure functions (no infra) so the translation is unit-testable. The SDK emits ``gen_ai.*`` attribute
dicts (see :func:`tally.schema.build_span_attributes`); a span may additionally carry structural
keys (``TraceId``/``trace_id``, ``SpanId``, ``Timestamp``, ``ServiceName``, ...). High-value
attributes are promoted to typed columns; everything else lands in the ``SpanAttributes`` map.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tally.schema import GenAI, micro_to_usd
from tally.wire import uuid7

# gen_ai.* keys that get promoted to typed columns (so they don't also duplicate into the map).
_PROMOTED_GENAI = frozenset(
    {
        GenAI.FEATURE_TAG,
        GenAI.SESSION_ID,
        GenAI.USER_ID_HASH,
        GenAI.USER_ID_HASH_KEY_VERSION,
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
    }
)

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


def _fixed64(v: object | None) -> str:
    """FixedString(64) wants exactly 64 bytes; ClickHouse pads, but truncate over-long input."""
    s = _s(v)
    return s[:64]


def span_to_row(
    span: dict[str, object],
    *,
    tenant_id: str,
    effective_ts_ns: int,
    sample_rate: float = 1.0,
) -> tuple[object, ...]:
    """Translate one enriched span attribute dict into an ``otel_spans`` row tuple.

    ``effective_ts_ns`` is the skew-clamped timestamp from :func:`tally.timekeeping.assess`. Cost is
    converted from integer micro-USD back to a :class:`~decimal.Decimal` for the Decimal64(8) column.
    """
    ts = datetime.fromtimestamp(effective_ts_ns / 1e9, tz=timezone.utc)

    cost_micro = span.get(GenAI.COST_ESTIMATED_MICRO_USD)
    estimated_cost: Decimal = (
        micro_to_usd(cost_micro) if isinstance(cost_micro, int) and not isinstance(cost_micro, bool)
        else Decimal(0)
    )

    # Long-tail attributes: anything not promoted and not structural, stringified for Map(String,String).
    extra: dict[str, str] = {}
    for k, v in span.items():
        if k in _PROMOTED_GENAI or k in _STRUCTURAL or v is None:
            continue
        extra[str(k)] = str(v)

    return (
        tenant_id,
        ts,
        _s(_pick(span, "TraceId", "trace_id") or uuid7().replace("-", "")),
        _s(_pick(span, "SpanId", "span_id") or uuid7().replace("-", "")[:16]),
        _s(_pick(span, "ParentSpanId", "parent_span_id")),
        _s(_pick(span, "ServiceName", "service_name") or "unknown"),
        _s(_pick(span, "SpanName", "span_name") or span.get(GenAI.OPERATION_NAME) or "llm.call"),
        _i(_pick(span, "StatusCode", "status_code")),
        _i(_pick(span, "DurationNs", "duration_ns")),
        _s(span.get(GenAI.FEATURE_TAG) or "untagged"),
        _s(span.get(GenAI.SESSION_ID)),
        _fixed64(span.get(GenAI.USER_ID_HASH)),
        _s(span.get(GenAI.USER_ID_HASH_KEY_VERSION)),
        _s(span.get(GenAI.IDEMPOTENCY_KEY)),
        _s(span.get(GenAI.SYSTEM)),
        _s(span.get(GenAI.REQUEST_MODEL)),
        _s(span.get(GenAI.RESPONSE_MODEL)),
        _s(span.get(GenAI.OPERATION_NAME)),
        _s(span.get(GenAI.TOOL_NAME)),
        _i(span.get(GenAI.USAGE_INPUT_TOKENS)),
        _i(span.get(GenAI.USAGE_OUTPUT_TOKENS)),
        _i(span.get(GenAI.USAGE_CACHED_INPUT_TOKENS)),
        estimated_cost,
        _s(span.get(GenAI.COST_CURRENCY) or "USD"),
        "estimated",
        _s(span.get(GenAI.COST_PRICE_CATALOG_VERSION)),
        _s(span.get(GenAI.AGENT_RUN_ID)),
        _i(span.get(GenAI.AGENT_STEP_INDEX)),
        extra,
        float(sample_rate),
    )
