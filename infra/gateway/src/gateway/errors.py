"""Stable gateway error codes: the wire contract for rejections.

These strings are part of the public ingest contract: clients branch on them (e.g. the SDK egress
loop drops 4xx-class items but retries QUOTA_EXCEEDED/RATE_LIMITED honoring ``retry_after``). Keep
them additive: never rename or repurpose an existing code.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # --- auth / tenancy (CTO-33) ---
    UNAUTHENTICATED = "UNAUTHENTICATED"        # missing/invalid/revoked bearer key
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"        # key lacks the scope for this operation
    TENANT_MISMATCH = "TENANT_MISMATCH"        # body claims a tenant the key isn't bound to
    HMAC_EXPORT_DISABLED = "HMAC_EXPORT_DISABLED"  # per-tenant policy forbids HMAC key export (Init 2 §3.2)
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"          # monthly tenant quota spent
    RATE_LIMITED = "RATE_LIMITED"              # short-term per-tenant rate cap hit
    # CTO-245: the durable (tenant_id, batch_id) store could not answer, so the gateway cannot tell
    # a replay from a new batch. Retryable on purpose: accepting a batch we cannot dedup would
    # double-count its spend permanently, and no later query could tell which dollars were doubled.
    IDEMPOTENCY_UNAVAILABLE = "IDEMPOTENCY_UNAVAILABLE"

    # --- validation (CTO-34) ---
    INVALID_SCHEMA = "INVALID_SCHEMA"          # span/event fails OTel + extension schema
    PII_DETECTED = "PII_DETECTED"              # raw (un-hashed) user id / email present
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"    # item exceeds size cap
    UNKNOWN_FEATURE_TAG = "UNKNOWN_FEATURE_TAG"  # accepted-but-flagged (not a rejection)


# Codes reported on an item the gateway ACCEPTED: advice about the span, not a rejection of it
# (validation.py ``Verdict.flags``). Declared as a set, rather than left implicit in a comment on
# the member and a literal in app.py, so a client can mirror it and pin that mirror to this file.
# The SDK's partial-ack accounting has to tell an accepted-but-flagged item from a refusal, and one
# flag code it had not learned was enough to switch that accounting off for a whole ack (CTO-406).
# Keep this in step with any code added above whose meaning is "accepted, with a note".
ACCEPTED_BUT_FLAGGED: frozenset[ErrorCode] = frozenset({ErrorCode.UNKNOWN_FEATURE_TAG})


# Codes that mean "do not retry this item as-is" (4xx-class). The rest are retryable.
NON_RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.UNAUTHENTICATED,
        ErrorCode.FORBIDDEN_SCOPE,
        ErrorCode.TENANT_MISMATCH,
        ErrorCode.INVALID_SCHEMA,
        ErrorCode.PII_DETECTED,
        ErrorCode.PAYLOAD_TOO_LARGE,
    }
)
