"""Per-item span validation + PII rejection at the ingest boundary (CTO-34, spec §4.5/§12.6).

Garbage in poisons every downstream workflow, and raw PII must never land in storage. Validation
happens here, per span, so one bad item never fails the whole batch.

Three rejection classes (item dropped) and one non-fatal flag:

* ``INVALID_SCHEMA``   — span isn't a dict, or a *known* gen_ai typed field has the wrong type /
  negative value / malformed currency or operation. Unknown keys are tolerated (additive-only
  contract, CTO-31) — they pass through to the ``SpanAttributes`` map.
* ``PII_DETECTED``     — a raw e-mail appears in any string value, ``user_id_hash`` looks un-hashed,
  or a forbidden raw-PII key (``email``, ``user.email``, ...) is present. The SDK is supposed to
  hash before egress; this is the server-side backstop.
* ``PAYLOAD_TOO_LARGE`` — the JSON-serialized span exceeds ``max_span_bytes``.
* ``UNKNOWN_FEATURE_TAG`` (flag, not a rejection) — a feature tag the tenant never declared. We keep
  the span but surface the flag so the dashboard can prompt the customer to declare it.

This module is pure (no infra) and unit-tested without a stack.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from tally.schema import GenAI

from gateway.errors import ErrorCode

# An e-mail anywhere in a string value is treated as raw PII.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Keys that must never carry raw identifiers — their mere presence is a PII violation.
_FORBIDDEN_PII_KEYS = frozenset(
    {
        "email", "user_email", "user.email", "gen_ai.user.email",
        "user_id", "gen_ai.user.id", "phone", "phone_number", "ssn",
    }
)

# gen_ai typed-int keys and the value ceilings we sanity-check. (Mirrors tally.schema._INT_KEYS.)
_INT_KEYS = frozenset(
    {
        GenAI.USAGE_INPUT_TOKENS, GenAI.USAGE_OUTPUT_TOKENS, GenAI.USAGE_CACHED_INPUT_TOKENS,
        GenAI.COST_ESTIMATED_MICRO_USD, GenAI.TOOL_COST_MICRO_USD,
        GenAI.AGENT_STEP_INDEX, GenAI.AGENT_STEP_MAX,
    }
)

# gen_ai keys that must be non-empty strings when present.
_STR_KEYS = frozenset(
    {
        GenAI.SYSTEM, GenAI.REQUEST_MODEL, GenAI.RESPONSE_MODEL, GenAI.OPERATION_NAME,
        GenAI.COST_CURRENCY, GenAI.FEATURE_TAG, GenAI.SESSION_ID, GenAI.USER_ID_HASH,
    }
)

DEFAULT_MAX_SPAN_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ItemResult:
    """Outcome for one span. ``accepted`` items are written; ``rejection`` items are dropped.

    ``flags`` are non-fatal (e.g. UNKNOWN_FEATURE_TAG): the item is still accepted but the flag is
    reported back so the client/dashboard can act.
    """

    accepted: bool
    rejection: ErrorCode | None = None
    message: str = ""
    flags: list[ErrorCode] = field(default_factory=list)


def _iter_str_values(span: dict[str, object]) -> Iterable[tuple[str, str]]:
    for k, v in span.items():
        if isinstance(v, str):
            yield str(k), v


def _looks_unhashed(user_id_hash: str) -> bool:
    """A real HMAC-SHA256 hex is 64 lowercase hex chars. Reject obvious raw ids / e-mails.

    Heuristic, deliberately conservative to avoid false positives: only flag when the value clearly
    isn't a hash — contains '@', or contains non-hex characters (a raw username/id would).
    """
    if "@" in user_id_hash:
        return True
    s = user_id_hash.strip().lower()
    if not s:
        return False
    is_hexish = all(c in "0123456789abcdef" for c in s) and len(s) >= 16
    return not is_hexish


class SpanValidator:
    """Validates one span at a time. Construct once per request with the tenant's known feature tags.

    ``known_feature_tags=None`` disables the unknown-tag flag (used when the tenant's declared tags
    aren't available); pass a set to enable it.
    """

    def __init__(
        self,
        *,
        max_span_bytes: int = DEFAULT_MAX_SPAN_BYTES,
        known_feature_tags: set[str] | None = None,
    ) -> None:
        self._max_bytes = max_span_bytes
        self._known_tags = known_feature_tags

    def validate(self, span: object) -> ItemResult:
        if not isinstance(span, dict):
            return ItemResult(False, ErrorCode.INVALID_SCHEMA, "span is not an object")

        # 1) size — cheap reject before deeper inspection.
        try:
            size = len(json.dumps(span, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return ItemResult(False, ErrorCode.INVALID_SCHEMA, "span is not JSON-serializable")
        if size > self._max_bytes:
            return ItemResult(
                False, ErrorCode.PAYLOAD_TOO_LARGE, f"span {size}B exceeds {self._max_bytes}B cap"
            )

        # 2) PII — forbidden keys, raw e-mails, un-hashed user id.
        pii = self._detect_pii(span)
        if pii is not None:
            return ItemResult(False, ErrorCode.PII_DETECTED, pii)

        # 3) schema — only *known* typed keys are checked; unknown keys pass through.
        schema_err = self._check_schema(span)
        if schema_err is not None:
            return ItemResult(False, ErrorCode.INVALID_SCHEMA, schema_err)

        # 4) non-fatal flags.
        flags: list[ErrorCode] = []
        tag = span.get(GenAI.FEATURE_TAG)
        if self._known_tags is not None and isinstance(tag, str) and tag and tag not in self._known_tags:
            flags.append(ErrorCode.UNKNOWN_FEATURE_TAG)

        return ItemResult(True, flags=flags)

    def _detect_pii(self, span: dict[str, object]) -> str | None:
        for key in span:
            if str(key).lower() in _FORBIDDEN_PII_KEYS:
                return f"forbidden raw-PII key present: {key!r}"
        for key, value in _iter_str_values(span):
            if key == GenAI.USER_ID_HASH:
                if _looks_unhashed(value):
                    return "gen_ai.user_id_hash is not a hash (raw identifier?)"
                continue
            if _EMAIL_RE.search(value):
                return f"raw e-mail detected in {key!r}"
        return None

    def _check_schema(self, span: dict[str, object]) -> str | None:
        for key, value in span.items():
            if key in _INT_KEYS:
                if isinstance(value, bool) or not isinstance(value, int):
                    return f"{key} must be int, got {type(value).__name__}"
                if value < 0:
                    return f"{key} must be >= 0, got {value}"
            elif key in _STR_KEYS:
                if not isinstance(value, str):
                    return f"{key} must be str, got {type(value).__name__}"
                if value == "":
                    return f"{key} must be non-empty"
        op = span.get(GenAI.OPERATION_NAME)
        if isinstance(op, str) and op and op != op.lower():
            return f"{GenAI.OPERATION_NAME} must be lowercase, got {op!r}"
        currency = span.get(GenAI.COST_CURRENCY)
        if isinstance(currency, str) and not (len(currency) == 3 and currency.isalpha()):
            return f"{GenAI.COST_CURRENCY} must be a 3-letter ISO-4217 code, got {currency!r}"
        return None


def span_item_id(span: dict[str, object], index: int) -> str:
    """A stable id for per-item error reporting: trace:span if present, else the batch index."""
    trace = span.get("TraceId") or span.get("trace_id")
    sp = span.get("SpanId") or span.get("span_id")
    if trace and sp:
        return f"{trace}:{sp}"
    return f"#{index}"
