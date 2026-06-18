# SPDX-License-Identifier: Apache-2.0
"""Span schema — OpenTelemetry ``gen_ai.*`` semantic conventions plus ai-tally extensions.

A single namespace (``gen_ai.*``). We do not fork the convention; our additions are namespaced
under ``gen_ai.*`` and proposed upstream where missing (notably cost).

Cost on the wire is an **integer number of micro-USD** (1e-6 USD). This avoids floating-point on
the network and matches the Decimal64(8) storage choice. Use :func:`usd_to_micro` /
:func:`micro_to_usd` at the boundary.

The authoritative list of keys lives in :class:`GenAI`. :func:`build_span_attributes` produces a
conformant attribute dict; :func:`validate_span_attributes` checks an arbitrary dict against the
schema and returns a list of human-readable violations (empty == conformant).

Implements CTO-47.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class GenAI:
    """Attribute keys. Standard OTel semconv + ai-tally extensions (all ``gen_ai.*``)."""

    # --- Standard OTel GenAI semantic conventions ---
    SYSTEM = "gen_ai.system"  # e.g. "openai", "anthropic"
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"
    OPERATION_NAME = "gen_ai.operation.name"  # e.g. "chat", "embeddings", "tool"
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_CACHED_INPUT_TOKENS = "gen_ai.usage.cached_input_tokens"

    # --- ai-tally extensions (proposed upstream) ---
    COST_ESTIMATED_MICRO_USD = "gen_ai.cost.estimated_micro_usd"  # int, micro-USD
    COST_CURRENCY = "gen_ai.cost.currency"  # ISO-4217, default "USD"
    COST_PRICE_CATALOG_VERSION = "gen_ai.cost.price_catalog_version"

    FEATURE_TAG = "gen_ai.feature_tag"
    SESSION_ID = "gen_ai.session_id"
    USER_ID_HASH = "gen_ai.user_id_hash"  # HMAC-SHA256 hex
    USER_ID_HASH_KEY_VERSION = "gen_ai.user_id_hash_key_version"
    IDEMPOTENCY_KEY = "gen_ai.idempotency_key"

    AGENT_RUN_ID = "gen_ai.agent.run_id"
    AGENT_STEP_INDEX = "gen_ai.agent.step.index"
    AGENT_STEP_MAX = "gen_ai.agent.step.max_steps"

    TOOL_NAME = "gen_ai.tool.name"
    TOOL_CALL_ID = "gen_ai.tool.call_id"
    TOOL_COST_MICRO_USD = "gen_ai.tool.cost_micro_usd"

    RESOLVED_CONTEXT_REF = "gen_ai.resolved_context_ref"

    # Context-window drop signals (CTO-118). Counts/token counts ONLY — never the dropped
    # message text. Matches the bar set by the edge-proxy: no field here could hold a prompt.
    CONTEXT_DROPPED_MESSAGES = "gen_ai.context.dropped_messages"  # int, count of trimmed messages
    CONTEXT_DROPPED_TOKENS = "gen_ai.context.dropped_tokens"  # int, total tokens trimmed
    CONTEXT_WINDOW_USED_PCT = "gen_ai.context.window_used_pct"  # float, 0..1

    # Stratified-sampling provenance (CTO-119). The stratum the head-time sampler placed this trace
    # in ("body" | "mid" | "tail") plus the stratum's configured keep rate. Distinct from the
    # existing per-span `SampleRate` weight used for billing extrapolation — this pair lets the DQ
    # surface compute per-stratum confidence bands without inferring them from cost histograms.
    SAMPLING_STRATUM = "gen_ai.sampling.stratum"  # str, "body" | "mid" | "tail"
    SAMPLING_RATE = "gen_ai.sampling.rate"  # float, 0..1


# Known operation names (open set — unknown values are allowed but should be lowercase tokens).
OPERATIONS = frozenset({"chat", "completion", "embeddings", "tool", "agent", "rerank"})

#: Default currency when none supplied.
DEFAULT_CURRENCY = "USD"

# Expected python types per key. ``int`` keys must not be ``bool``.
_INT_KEYS = frozenset(
    {
        GenAI.USAGE_INPUT_TOKENS,
        GenAI.USAGE_OUTPUT_TOKENS,
        GenAI.USAGE_CACHED_INPUT_TOKENS,
        GenAI.COST_ESTIMATED_MICRO_USD,
        GenAI.TOOL_COST_MICRO_USD,
        GenAI.AGENT_STEP_INDEX,
        GenAI.AGENT_STEP_MAX,
        GenAI.CONTEXT_DROPPED_MESSAGES,
        GenAI.CONTEXT_DROPPED_TOKENS,
    }
)
# Float keys — context-window utilization (CTO-118) and stratum keep-rate (CTO-119).
_FLOAT_KEYS = frozenset({GenAI.CONTEXT_WINDOW_USED_PCT, GenAI.SAMPLING_RATE})
_STR_KEYS = frozenset(
    {
        GenAI.SYSTEM,
        GenAI.REQUEST_MODEL,
        GenAI.RESPONSE_MODEL,
        GenAI.OPERATION_NAME,
        GenAI.COST_CURRENCY,
        GenAI.COST_PRICE_CATALOG_VERSION,
        GenAI.FEATURE_TAG,
        GenAI.SESSION_ID,
        GenAI.USER_ID_HASH,
        GenAI.USER_ID_HASH_KEY_VERSION,
        GenAI.IDEMPOTENCY_KEY,
        GenAI.AGENT_RUN_ID,
        GenAI.TOOL_NAME,
        GenAI.TOOL_CALL_ID,
        GenAI.RESOLVED_CONTEXT_REF,
        GenAI.SAMPLING_STRATUM,
    }
)
# Allowed values for the stratum string. The validator rejects anything else so we don't end up
# with a long-tail of free-text strata polluting the DQ table.
_SAMPLING_STRATA = frozenset({"body", "mid", "tail"})
_ALL_KEYS = _INT_KEYS | _STR_KEYS | _FLOAT_KEYS

_MICRO = Decimal(1_000_000)


def usd_to_micro(amount_usd: Decimal | str | int) -> int:
    """Convert a USD amount to integer micro-USD (round half-up at the 6th decimal)."""
    d = amount_usd if isinstance(amount_usd, Decimal) else Decimal(str(amount_usd))
    return int((d * _MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micro_to_usd(micro: int) -> Decimal:
    """Convert integer micro-USD back to a USD :class:`~decimal.Decimal`."""
    return (Decimal(micro) / _MICRO).quantize(Decimal("0.00000001"))


@dataclass(slots=True)
class SpanFields:
    """Typed convenience holder for the common fields. All optional; ``build_span_attributes``
    emits only the keys that are set (non-None)."""

    system: str | None = None
    request_model: str | None = None
    response_model: str | None = None
    operation: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_estimated_micro_usd: int | None = None
    cost_currency: str | None = None
    price_catalog_version: str | None = None
    feature_tag: str | None = None
    session_id: str | None = None
    user_id_hash: str | None = None
    user_id_hash_key_version: str | None = None
    idempotency_key: str | None = None
    agent_run_id: str | None = None
    agent_step_index: int | None = None
    agent_step_max: int | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_cost_micro_usd: int | None = None
    resolved_context_ref: str | None = None
    sampling_stratum: str | None = None
    sampling_rate: float | None = None


_FIELD_TO_KEY = {
    "system": GenAI.SYSTEM,
    "request_model": GenAI.REQUEST_MODEL,
    "response_model": GenAI.RESPONSE_MODEL,
    "operation": GenAI.OPERATION_NAME,
    "input_tokens": GenAI.USAGE_INPUT_TOKENS,
    "output_tokens": GenAI.USAGE_OUTPUT_TOKENS,
    "cached_input_tokens": GenAI.USAGE_CACHED_INPUT_TOKENS,
    "cost_estimated_micro_usd": GenAI.COST_ESTIMATED_MICRO_USD,
    "cost_currency": GenAI.COST_CURRENCY,
    "price_catalog_version": GenAI.COST_PRICE_CATALOG_VERSION,
    "feature_tag": GenAI.FEATURE_TAG,
    "session_id": GenAI.SESSION_ID,
    "user_id_hash": GenAI.USER_ID_HASH,
    "user_id_hash_key_version": GenAI.USER_ID_HASH_KEY_VERSION,
    "idempotency_key": GenAI.IDEMPOTENCY_KEY,
    "agent_run_id": GenAI.AGENT_RUN_ID,
    "agent_step_index": GenAI.AGENT_STEP_INDEX,
    "agent_step_max": GenAI.AGENT_STEP_MAX,
    "tool_name": GenAI.TOOL_NAME,
    "tool_call_id": GenAI.TOOL_CALL_ID,
    "tool_cost_micro_usd": GenAI.TOOL_COST_MICRO_USD,
    "resolved_context_ref": GenAI.RESOLVED_CONTEXT_REF,
    "sampling_stratum": GenAI.SAMPLING_STRATUM,
    "sampling_rate": GenAI.SAMPLING_RATE,
}


def build_span_attributes(fields: SpanFields) -> dict[str, object]:
    """Build a conformant attribute dict from :class:`SpanFields`.

    Only set (non-None) fields are emitted. ``cost_currency`` defaults to ``USD`` whenever any cost
    is present. The result is guaranteed to pass :func:`validate_span_attributes`.
    """
    attrs: dict[str, object] = {}
    for field_name, key in _FIELD_TO_KEY.items():
        value = getattr(fields, field_name)
        if value is not None:
            attrs[key] = value
    if GenAI.COST_ESTIMATED_MICRO_USD in attrs and GenAI.COST_CURRENCY not in attrs:
        attrs[GenAI.COST_CURRENCY] = DEFAULT_CURRENCY
    return attrs


def validate_span_attributes(attrs: dict[str, object]) -> list[str]:
    """Return a list of conformance violations (empty list == conformant).

    Checks: known keys only, correct value types (int keys reject ``bool`` and floats),
    non-negative token/cost integers, known-ish operation name, ISO-4217-shaped currency.
    """
    violations: list[str] = []

    for key, value in attrs.items():
        if key not in _ALL_KEYS:
            violations.append(f"unknown attribute key: {key!r}")
            continue
        if key in _INT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int):
                violations.append(f"{key} must be int, got {type(value).__name__}")
            elif value < 0:
                violations.append(f"{key} must be >= 0, got {value}")
        elif key in _STR_KEYS:
            if not isinstance(value, str):
                violations.append(f"{key} must be str, got {type(value).__name__}")
            elif value == "":
                violations.append(f"{key} must be non-empty")
        elif key in _FLOAT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append(f"{key} must be float, got {type(value).__name__}")
            elif not (0.0 <= float(value) <= 1.0):
                violations.append(f"{key} must be in [0, 1], got {value}")

    op = attrs.get(GenAI.OPERATION_NAME)
    if isinstance(op, str) and op and op != op.lower():
        violations.append(f"{GenAI.OPERATION_NAME} should be lowercase, got {op!r}")

    currency = attrs.get(GenAI.COST_CURRENCY)
    if isinstance(currency, str) and not (len(currency) == 3 and currency.isalpha()):
        violations.append(
            f"{GenAI.COST_CURRENCY} must be a 3-letter ISO-4217 code, got {currency!r}"
        )

    if GenAI.COST_ESTIMATED_MICRO_USD in attrs and GenAI.COST_CURRENCY not in attrs:
        violations.append("cost present without gen_ai.cost.currency")

    stratum = attrs.get(GenAI.SAMPLING_STRATUM)
    if isinstance(stratum, str) and stratum not in _SAMPLING_STRATA:
        violations.append(
            f"{GenAI.SAMPLING_STRATUM} must be one of {sorted(_SAMPLING_STRATA)}, got {stratum!r}"
        )

    return violations
