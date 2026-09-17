# SPDX-License-Identifier: Apache-2.0
"""Span schema: OpenTelemetry ``gen_ai.*`` semantic conventions plus ai-tally extensions.

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

import secrets
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

    #: HOW the call was billed, which is a different question from what it cost (CTO-417).
    #: See BILLING_MODES below for the values and the full contract. Sits in the cost namespace
    #: because it is an input to the cost decision, alongside the catalog version that records
    #: which rates produced a figure.
    COST_BILLING_MODE = "gen_ai.cost.billing_mode"  # str, "api" | "subscription"

    FEATURE_TAG = "gen_ai.feature_tag"
    SESSION_ID = "gen_ai.session_id"
    USER_ID_HASH = "gen_ai.user_id_hash"  # HMAC-SHA256 hex
    USER_ID_HASH_KEY_VERSION = "gen_ai.user_id_hash_key_version"

    # Account dimension (CTO-181 / B2 of the cost-per-customer plan). The tenant's own customer,
    # sitting between TenantId (the ai-tally customer) and UserIdHash (one end user). Hashed
    # exactly like a user id: HMAC-SHA256 hex under the per-tenant key, with the key version
    # travelling alongside so a rotation (CTO-74) does not orphan history. The raw account id
    # never leaves the process.
    ACCOUNT_ID_HASH = "gen_ai.account_id_hash"  # HMAC-SHA256 hex
    ACCOUNT_ID_HASH_KEY_VERSION = "gen_ai.account_id_hash_key_version"
    # Optional human-readable label for the account. WIRE-ONLY: the gateway upserts it into the
    # Postgres label store (CTO-186) keyed on the account hash and does NOT write it to the span
    # row. Labels are mutable metadata and a customer name has no business in the telemetry
    # store, which is the whole reason the id is hashed. Emitted only when an account hash is.
    ACCOUNT_LABEL = "gen_ai.account_label"
    IDEMPOTENCY_KEY = "gen_ai.idempotency_key"

    AGENT_RUN_ID = "gen_ai.agent.run_id"
    AGENT_STEP_INDEX = "gen_ai.agent.step.index"
    AGENT_STEP_MAX = "gen_ai.agent.step.max_steps"

    TOOL_NAME = "gen_ai.tool.name"
    TOOL_CALL_ID = "gen_ai.tool.call_id"
    TOOL_COST_MICRO_USD = "gen_ai.tool.cost_micro_usd"

    RESOLVED_CONTEXT_REF = "gen_ai.resolved_context_ref"

    # Context-window drop signals (CTO-118). Counts/token counts ONLY, never the dropped
    # message text. Matches the bar set by the edge-proxy: no field here could hold a prompt.
    CONTEXT_DROPPED_MESSAGES = "gen_ai.context.dropped_messages"  # int, count of trimmed messages
    CONTEXT_DROPPED_TOKENS = "gen_ai.context.dropped_tokens"  # int, total tokens trimmed
    CONTEXT_WINDOW_USED_PCT = "gen_ai.context.window_used_pct"  # float, 0..1

    # Stratified-sampling provenance (CTO-119). The stratum the head-time sampler placed this trace
    # in ("body" | "mid" | "tail") plus the stratum's configured keep rate. Distinct from the
    # existing per-span `SampleRate` weight used for billing extrapolation; this pair lets the DQ
    # surface compute per-stratum confidence bands without inferring them from cost histograms.
    SAMPLING_STRATUM = "gen_ai.sampling.stratum"  # str, "body" | "mid" | "tail"
    SAMPLING_RATE = "gen_ai.sampling.rate"  # float, 0..1


#: Structural wire keys for the span's identity (CTO-396). These are not ``gen_ai.*`` attributes:
#: they identify the span itself, travel in the same dict, and the gateway promotes them to the
#: ``otel_spans`` TraceId / SpanId columns (``gateway.mapping.span_to_row``). The spelling matches
#: what the edge proxy already sends (``infra/edge-proxy/internal/telemetry/telemetry.go``) so a
#: proxied span and an SDK span land in the same columns.
TRACE_ID_KEY = "trace_id"
SPAN_ID_KEY = "span_id"

#: CTO-401: True only on a span whose trace id this SDK MINTED because the caller had no active
#: trace. CTO-396 gave every trace-less span its own fresh trace id so it would stop colliding with
#: every other trace-less span on the wire, which is right for storage and wrong for the head meter:
#: a per-span id is not a trace a customer started, and counting one billable trace per trace-less
#: span turned traffic that contributed zero into traffic that contributes one each. The id stays on
#: the wire (the ClickHouse-derived invoice count is unchanged); this flag is what lets the gateway
#: head meter tell a real trace from a synthetic one instead of guessing from the id's shape.
#: Absent on spans carrying a caller's real trace id, so its absence means "real", not "unknown".
#:
#: STORED SPELLING (CTO-401 review). This is a long-tail attribute, so the gateway writes it
#: into the ClickHouse ``SpanAttributes`` Map(String, String) through ``str(value)``
#: (``gateway.mapping.span_to_row``), which renders a Python ``True`` as the string ``'True'``,
#: NOT ``'true'``. A query written as ``SpanAttributes['gen_ai.trace_id_synthetic'] = 'true'``
#: therefore matches nothing and returns a clean empty result, which looks exactly like "no
#: synthetic spans". Match ``'True'``. The gateway's own head meter never reads the stored string
#: (it reads the wire value before mapping), so this spelling is a query concern only.
TRACE_ID_SYNTHETIC_KEY = "gen_ai.trace_id_synthetic"

#: When the span was metered, in nanoseconds since the epoch (CTO-404). Structural like the ids, and
#: the spelling the gateway already reads: ``gateway.mapping`` lists ``timestamp_ns`` as structural,
#: and ``gateway.app`` prefers it over the batch's ``client_send_ts_ns``. A span that carries none
#: inherits the ENVELOPE's send time, which is different in every envelope, so the same span
#: re-enveloped after a restart lands on a different ClickHouse sorting key and the
#: ReplacingMergeTree keeps both rows instead of collapsing them.
TIMESTAMP_NS_KEY = "timestamp_ns"

#: CTO-417. ``gen_ai.cost.billing_mode`` values. ai-tally prices from (provider, model, tokens) and
#: until now had no notion of HOW a call was billed, so a call covered by a subscription whose model
#: happens to be in the catalog was priced at pay-as-you-go list rates and stamped
#: ``CostSource = 'estimated'``: a confident assertion of a cost the customer never incurred. That
#: is the same class of fabricated number the Nullable cost columns exist to remove (CTO-244), only
#: arrived at from the other direction.
#:
#: * ``"api"`` (or the key ABSENT): per-token API spend. Priced from the catalog exactly as before.
#: * ``"subscription"``: covered by a seat or plan the customer already pays for. There is no
#:   per-call price to know, so NO cost is assigned, whether or not the catalog prices the model.
#:   The row lands ``EstimatedCost = NULL`` with ``CostSource = 'subscription'``.
#:
#: ABSENCE MEANS "PRICE IT AS WE DO NOW", never "unknown, refuse to price". The wire contract is
#: additive-only (CTO-31) and every producer shipped before this field existed sends nothing, so
#: reading absence as unknown would stop pricing all of them at once.
#:
#: WHY NOT PER TENANT OR PER PROVIDER. A single tenant genuinely mixes the two: the motivating
#: tenant's Fireworks traffic is per-token API spend while its openai and claude-code traffic runs
#: under a subscription. A tenant-level or provider-level switch cannot express that, so the mode
#: belongs on the span.
#:
#: THIS MARKER IS CLIENT-ASSERTED AND CANNOT BE CORROBORATED SERVER-SIDE. Nothing in the telemetry
#: proves a call was covered by a subscription; ai-tally only knows what the producer said, exactly
#: as with ``gen_ai.trace_id_synthetic`` (CTO-401). Marking a span subscription REMOVES cost from
#: the tenant's own spend picture, so a wrong or adversarial marker understates their reported
#: spend. That is acceptable for observability and is NOT acceptable as a billing control: do not
#: build enforcement, invoicing or entitlement checks on this field without an independent source
#: of truth. CTO-410 tracks the general problem of client-asserted fields the gateway cannot verify.
BILLING_MODE_API = "api"
BILLING_MODE_SUBSCRIPTION = "subscription"
BILLING_MODES = frozenset({BILLING_MODE_API, BILLING_MODE_SUBSCRIPTION})

_STRUCTURAL_KEYS = frozenset({TRACE_ID_KEY, SPAN_ID_KEY})
#: Structural keys whose value is an integer rather than a string identifier.
_STRUCTURAL_INT_KEYS = frozenset({TIMESTAMP_NS_KEY})


def new_span_id() -> str:
    """Generate a span id: 8 crypto-random bytes as lowercase hex (CTO-396).

    Same shape and same source of randomness as the edge proxy's ``randomHex(8)``. A span id only
    has to be unique; it carries no meaning and must encode nothing about the request or the
    customer, which is why this is raw randomness rather than a hash of anything.
    """
    return secrets.token_hex(8)


# Known operation names (open set; unknown values are allowed but should be lowercase tokens).
OPERATIONS = frozenset(
    {"chat", "completion", "embeddings", "tool", "agent", "rerank", "vector"}
)

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
# Float keys: context-window utilization (CTO-118) and stratum keep-rate (CTO-119).
_FLOAT_KEYS = frozenset({GenAI.CONTEXT_WINDOW_USED_PCT, GenAI.SAMPLING_RATE})
_STR_KEYS = frozenset(
    {
        GenAI.SYSTEM,
        GenAI.REQUEST_MODEL,
        GenAI.RESPONSE_MODEL,
        GenAI.OPERATION_NAME,
        GenAI.COST_CURRENCY,
        GenAI.COST_PRICE_CATALOG_VERSION,
        GenAI.COST_BILLING_MODE,
        GenAI.FEATURE_TAG,
        GenAI.SESSION_ID,
        GenAI.USER_ID_HASH,
        GenAI.USER_ID_HASH_KEY_VERSION,
        GenAI.ACCOUNT_ID_HASH,
        GenAI.ACCOUNT_ID_HASH_KEY_VERSION,
        GenAI.ACCOUNT_LABEL,
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
# Bool keys (CTO-401). The first of its kind, which is why there was no such category before:
# every other attribute is a count, a rate or a string. ``_INT_KEYS`` deliberately REJECTS bool
# (a bool is an int in Python and a token count of ``True`` is a bug), so a bool key needs its
# own bucket rather than riding along in that one.
_BOOL_KEYS = frozenset({TRACE_ID_SYNTHETIC_KEY})
_ALL_KEYS = _INT_KEYS | _STR_KEYS | _FLOAT_KEYS | _BOOL_KEYS

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
    billing_mode: str | None = None
    feature_tag: str | None = None
    session_id: str | None = None
    user_id_hash: str | None = None
    user_id_hash_key_version: str | None = None
    account_id_hash: str | None = None
    account_id_hash_key_version: str | None = None
    account_label: str | None = None
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
    "billing_mode": GenAI.COST_BILLING_MODE,
    "feature_tag": GenAI.FEATURE_TAG,
    "session_id": GenAI.SESSION_ID,
    "user_id_hash": GenAI.USER_ID_HASH,
    "user_id_hash_key_version": GenAI.USER_ID_HASH_KEY_VERSION,
    "account_id_hash": GenAI.ACCOUNT_ID_HASH,
    "account_id_hash_key_version": GenAI.ACCOUNT_ID_HASH_KEY_VERSION,
    "account_label": GenAI.ACCOUNT_LABEL,
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
        # CTO-396: the span's own identity rides in this dict alongside the attributes, so the
        # structural keys are conformant rather than "unknown". They must still be real, non-empty
        # strings: an empty id is the same collapse-into-one hazard as no id at all.
        if key in _STRUCTURAL_INT_KEYS:
            # CTO-404: the span's own metering time rides in this dict too. A negative or boolean
            # timestamp is not a time, and a wrong one here silently moves the row into another
            # rollup bucket, so it is validated rather than trusted.
            if isinstance(value, bool) or not isinstance(value, int):
                violations.append(f"{key} must be int, got {type(value).__name__}")
            elif value < 0:
                violations.append(f"{key} must be >= 0, got {value}")
            continue
        if key in _STRUCTURAL_KEYS:
            if not isinstance(value, str):
                violations.append(f"{key} must be str, got {type(value).__name__}")
            elif value == "":
                violations.append(f"{key} must be non-empty")
            continue
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
        elif key in _BOOL_KEYS:
            # A real bool, not a truthy string: the gateway matches on ``is True`` and the string
            # "false" would sail past that as a marked span. Absence is how "not synthetic" is
            # spelled, so an explicit False is permitted but means exactly what absence means.
            if not isinstance(value, bool):
                violations.append(f"{key} must be bool, got {type(value).__name__}")

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

    # CTO-417. Same treatment as the stratum above and for the same reason: a free-text billing mode
    # would be a silent no-op at the gateway (which prices only what it recognises), so the SDK says
    # so here rather than letting a typo look like it worked. Absence is conformant and means "price
    # it as we do now"; this only fires on a value that was spelled.
    billing_mode = attrs.get(GenAI.COST_BILLING_MODE)
    if isinstance(billing_mode, str) and billing_mode and billing_mode not in BILLING_MODES:
        violations.append(
            f"{GenAI.COST_BILLING_MODE} must be one of {sorted(BILLING_MODES)}, "
            f"got {billing_mode!r}"
        )

    stratum = attrs.get(GenAI.SAMPLING_STRATUM)
    if isinstance(stratum, str) and stratum not in _SAMPLING_STRATA:
        violations.append(
            f"{GenAI.SAMPLING_STRATUM} must be one of {sorted(_SAMPLING_STRATA)}, got {stratum!r}"
        )

    return violations
