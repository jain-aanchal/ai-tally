"""Stable gateway error codes — the wire contract for rejections.

These strings are part of the public ingest contract: clients branch on them (e.g. the SDK egress
loop drops 4xx-class items but retries QUOTA_EXCEEDED/RATE_LIMITED honoring ``retry_after``). Keep
them additive — never rename or repurpose an existing code.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # --- auth / tenancy (CTO-33) ---
    UNAUTHENTICATED = "UNAUTHENTICATED"        # missing/invalid/revoked bearer key
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"        # key lacks the scope for this operation
    TENANT_MISMATCH = "TENANT_MISMATCH"        # body claims a tenant the key isn't bound to
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"          # monthly tenant quota spent
    RATE_LIMITED = "RATE_LIMITED"              # short-term per-tenant rate cap hit

    # --- validation (CTO-34) ---
    INVALID_SCHEMA = "INVALID_SCHEMA"          # span/event fails OTel + extension schema
    PII_DETECTED = "PII_DETECTED"              # raw (un-hashed) user id / email present
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"    # item exceeds size cap
    UNKNOWN_FEATURE_TAG = "UNKNOWN_FEATURE_TAG"  # accepted-but-flagged (not a rejection)


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
