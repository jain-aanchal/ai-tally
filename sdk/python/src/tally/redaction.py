# SPDX-License-Identifier: Apache-2.0
"""PII redaction + per-tenant payload policy + right-to-deletion planning (CTO-76).

Why this module exists
----------------------
ai-tally runs as a **shared multi-tenant** service, which means one tenant's
payloads (prompts, completions, tool arguments) sit next to strangers'. Three
controls keep that safe, and this module implements the parts that are pure
logic and therefore belong in the SDK / a shared library:

1. **Configurable redactors** — detect and strip common PII (emails, phone
   numbers, card numbers, government IDs, IP addresses, secrets) from free-text
   payload fields *before* they leave the customer process.
2. **Per-tenant payload policy** — every tenant chooses how much payload we
   retain at all: ``FULL`` (keep, but still redact detected PII), ``HASHED``
   (keep only a tenant-scoped HMAC of the content so equality/joins survive but
   the plaintext does not), or ``NONE`` (drop content entirely).
3. **Right-to-deletion planning** — a tenant submits subject identifiers; we
   hash them (a raw identifier is never stored) and produce a deterministic
   :class:`DeletionPlan` enumerating the tables a worker must purge and the
   30-day SLA deadline. Executing the DML is a storage-plane concern and lives
   outside this module (see "Deferred" below).

Security invariants honoured here
---------------------------------
* HMAC keys are **per-tenant** and supplied by an injected
  :class:`HmacKeyProvider` (KMS-backed in production). The default in-memory
  provider exists only so dev/test never need running infra. Raw keys are never
  logged or embedded.
* Raw subject identifiers are **never** retained — deletion plans carry only the
  hashed forms.

Deferred (still infra-bound, tracked on CTO-76)
-----------------------------------------------
* The actual ``DELETE`` mutations across ClickHouse/Postgres tables.
* Region-pinned shared clusters + gateway routing by tenant region.

Nothing here raises on boundary junk: unknown attribute types pass through and
``None`` text redacts to an empty result. The whole point is to be safe to call
on the hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

#: Default right-to-deletion SLA (GDPR/CCPA practice: act within 30 days).
DEFAULT_DELETION_SLA_DAYS = 30

#: Tables that hold tenant subject data and must be purged on deletion.
#: Mirrors the storage schema (otel_spans + the business/identity tables).
DELETION_TARGET_TABLES: tuple[str, ...] = (
    "otel_spans",
    "business_events",
    "attribution_records",
    "identity_graph",
    "last_touch_index",
)


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Detector:
    """A named PII pattern and the token that replaces each match.

    ``token`` is the placeholder written in place of a match, e.g.
    ``[REDACTED:EMAIL]``. ``pattern`` is a compiled, case-insensitive regex.
    """

    name: str
    pattern: re.Pattern[str]
    token: str

    def find_count(self, text: str) -> int:
        """Number of (non-overlapping) matches in *text*."""
        return len(self.pattern.findall(text))


def _d(name: str, regex: str, *, flags: int = re.IGNORECASE) -> Detector:
    return Detector(name=name, pattern=re.compile(regex, flags), token=f"[REDACTED:{name}]")


# Order matters: more specific / higher-entropy patterns first so they win
# before a broader pattern (e.g. a card number) can partially consume them.
_EMAIL = _d("EMAIL", r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}")
# JWT: three base64url segments separated by dots.
_JWT = _d("JWT", r"\beyJ[A-Z0-9_\-]+\.[A-Z0-9_\-]+\.[A-Z0-9_\-]+\b")
# AWS access key id.
_AWS_KEY = _d("AWS_ACCESS_KEY", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# 13-16 digit card numbers, optional spaces/dashes between 4-digit groups.
_CREDIT_CARD = _d("CREDIT_CARD", r"\b(?:\d[ \-]?){13,16}\b")
# US SSN.
_SSN = _d("SSN", r"\b\d{3}-\d{2}-\d{4}\b")
# IPv4.
_IPV4 = _d("IPV4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Loose international phone: optional +, then 7-15 digits with separators.
_PHONE = _d("PHONE", r"\+?\d[\d \-().]{6,}\d")

#: Built-in detectors applied (in order) by a default :class:`Redactor`.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    _EMAIL,
    _JWT,
    _AWS_KEY,
    _CREDIT_CARD,
    _SSN,
    _IPV4,
    _PHONE,
)


# --------------------------------------------------------------------------- #
# Redactor
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Outcome of redacting one string.

    ``text`` is the redacted string. ``findings`` maps detector name -> number
    of matches replaced. ``redacted`` is True iff anything was replaced.
    """

    text: str
    findings: Mapping[str, int]

    @property
    def redacted(self) -> bool:
        return bool(self.findings)

    @property
    def total_findings(self) -> int:
        return sum(self.findings.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "findings": dict(self.findings),
            "redacted": self.redacted,
            "total_findings": self.total_findings,
        }


class Redactor:
    """Applies an ordered set of :class:`Detector` s to free text.

    Construct with the default detector set, or pass ``detectors`` to override.
    ``disable`` names detectors to drop from whatever set is active — handy for
    tenants who, say, legitimately store IP addresses.
    """

    __slots__ = ("_detectors",)

    def __init__(
        self,
        detectors: Iterable[Detector] | None = None,
        *,
        disable: Iterable[str] = (),
    ) -> None:
        base = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS
        disabled = {name.upper() for name in disable}
        self._detectors = tuple(d for d in base if d.name.upper() not in disabled)

    @property
    def detector_names(self) -> tuple[str, ...]:
        return tuple(d.name for d in self._detectors)

    def redact_text(self, text: object) -> RedactionResult:
        """Redact a single value. Non-strings (and ``None``) become ``""``.

        Never raises; on the hot path a junk value must not take down a span.
        """
        if not isinstance(text, str) or not text:
            return RedactionResult(text="" if text is None else _coerce_str(text), findings={})
        findings: dict[str, int] = {}
        out = text
        for det in self._detectors:
            count = det.find_count(out)
            if count:
                findings[det.name] = findings.get(det.name, 0) + count
                out = det.pattern.sub(det.token, out)
        return RedactionResult(text=out, findings=findings)


def _coerce_str(value: object) -> str:
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return ""


# --------------------------------------------------------------------------- #
# HMAC hashing (for the HASHED payload policy)
# --------------------------------------------------------------------------- #
@runtime_checkable
class HmacKeyProvider(Protocol):
    """Supplies a per-tenant HMAC key. KMS-backed in production."""

    def key_for(self, tenant_id: str) -> bytes: ...

    def key_version(self, tenant_id: str) -> str: ...


class InMemoryHmacKeyProvider:
    """Default provider so dev/test never need KMS.

    Derives a deterministic per-tenant key from a process-local root secret.
    The root secret is held in memory only and is never persisted or logged.
    """

    __slots__ = ("_root", "_version")

    def __init__(self, root_secret: bytes | None = None, *, key_version: str = "v1") -> None:
        # A random root each process unless one is injected (tests pin it).
        self._root = root_secret if root_secret is not None else _random_root()
        self._version = key_version

    def key_for(self, tenant_id: str) -> bytes:
        return hmac.new(self._root, tenant_id.encode("utf-8"), hashlib.sha256).digest()

    def key_version(self, tenant_id: str) -> str:
        return self._version


def _random_root() -> bytes:
    import os

    return os.urandom(32)


def hmac_hash(value: str, key: bytes) -> str:
    """Tenant-scoped HMAC-SHA256 of *value*, hex-encoded."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Per-tenant payload policy
# --------------------------------------------------------------------------- #
class PayloadMode(str, Enum):
    """How much payload content a tenant retains.

    * ``FULL`` — keep content, but still strip detected PII via the redactor.
    * ``HASHED`` — replace content with a tenant-scoped HMAC; plaintext gone,
      equality/joins on identical content still work.
    * ``NONE`` — drop content fields entirely.
    """

    FULL = "full"
    HASHED = "hashed"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PayloadPolicy:
    """A tenant's content-handling policy.

    ``content_keys`` names the attribute keys that hold payload/free-text
    content (prompts, completions, tool args). Everything else passes through
    untouched regardless of mode. ``drop_marker`` is the placeholder written for
    dropped content under ``NONE`` (set to ``None`` to remove the key instead).
    """

    mode: PayloadMode = PayloadMode.FULL
    content_keys: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "gen_ai.prompt",
                "gen_ai.completion",
                "gen_ai.tool.arguments",
                "input",
                "output",
                "prompt",
                "completion",
            }
        )
    )
    drop_marker: str | None = "[DROPPED]"

    def __post_init__(self) -> None:
        if not isinstance(self.mode, PayloadMode):
            raise ValueError(f"mode must be a PayloadMode, got {self.mode!r}")


@dataclass(frozen=True, slots=True)
class PolicyApplication:
    """Result of applying a :class:`PayloadPolicy` to an attribute dict.

    ``attributes`` is the transformed dict. ``mode`` echoes the policy mode.
    ``findings`` aggregates redactor hits across all content keys (only
    populated for ``FULL``). ``hashed_keys`` / ``dropped_keys`` record which
    content keys were transformed.
    """

    attributes: dict[str, object]
    mode: PayloadMode
    findings: Mapping[str, int]
    hashed_keys: tuple[str, ...]
    dropped_keys: tuple[str, ...]

    @property
    def total_findings(self) -> int:
        return sum(self.findings.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "findings": dict(self.findings),
            "total_findings": self.total_findings,
            "hashed_keys": list(self.hashed_keys),
            "dropped_keys": list(self.dropped_keys),
        }


class PayloadPolicyEnforcer:
    """Applies a :class:`PayloadPolicy` to span/event attribute dicts.

    Construct with a :class:`Redactor` (for ``FULL`` mode) and an
    :class:`HmacKeyProvider` (for ``HASHED`` mode). Both default to the
    in-memory implementations so the SDK works with zero configuration.
    """

    __slots__ = ("_redactor", "_keys")

    def __init__(
        self,
        redactor: Redactor | None = None,
        key_provider: HmacKeyProvider | None = None,
    ) -> None:
        self._redactor = redactor if redactor is not None else Redactor()
        self._keys = key_provider if key_provider is not None else InMemoryHmacKeyProvider()

    def apply(
        self,
        tenant_id: str,
        attributes: Mapping[str, object],
        policy: PayloadPolicy,
    ) -> PolicyApplication:
        """Transform *attributes* per *policy*. Never mutates the input."""
        out: dict[str, object] = dict(attributes)
        findings: dict[str, int] = {}
        hashed: list[str] = []
        dropped: list[str] = []

        for key in policy.content_keys:
            if key not in out:
                continue
            value = out[key]
            if policy.mode is PayloadMode.NONE:
                if policy.drop_marker is None:
                    del out[key]
                else:
                    out[key] = policy.drop_marker
                dropped.append(key)
            elif policy.mode is PayloadMode.HASHED:
                key_bytes = self._keys.key_for(tenant_id)
                version = self._keys.key_version(tenant_id)
                digest = hmac_hash(_coerce_str(value), key_bytes)
                out[key] = f"hmac:{version}:{digest}"
                hashed.append(key)
            else:  # FULL — keep, but redact detected PII
                result = self._redactor.redact_text(value)
                out[key] = result.text
                for name, count in result.findings.items():
                    findings[name] = findings.get(name, 0) + count

        return PolicyApplication(
            attributes=out,
            mode=policy.mode,
            findings=findings,
            hashed_keys=tuple(hashed),
            dropped_keys=tuple(dropped),
        )


@runtime_checkable
class TenantPolicyStore(Protocol):
    """Resolves a tenant's payload policy."""

    def policy_for(self, tenant_id: str) -> PayloadPolicy: ...


class InMemoryTenantPolicyStore:
    """Per-tenant policy registry with a configurable default."""

    __slots__ = ("_by_tenant", "_default")

    def __init__(self, default: PayloadPolicy | None = None) -> None:
        self._by_tenant: dict[str, PayloadPolicy] = {}
        self._default = default if default is not None else PayloadPolicy()

    def set_policy(self, tenant_id: str, policy: PayloadPolicy) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        self._by_tenant[tenant_id] = policy

    def policy_for(self, tenant_id: str) -> PayloadPolicy:
        return self._by_tenant.get(tenant_id, self._default)


# --------------------------------------------------------------------------- #
# Right-to-deletion planning
# --------------------------------------------------------------------------- #
def hash_subject_id(subject_id: str) -> str:
    """SHA-256 hex of a subject identifier.

    Deletion targets are matched on this hash; a raw identifier is never stored.
    Plain SHA-256 (not HMAC) so the hash is reproducible across services and key
    rotations — it's an opaque lookup token, not a secret-bearing value.
    """
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    """A tenant's right-to-deletion submission.

    ``subject_ids`` are raw identifiers (user ids, emails, anonymous ids). They
    are hashed during planning and never retained in the resulting plan.
    """

    tenant_id: str
    subject_ids: tuple[str, ...]
    received_at: datetime

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.subject_ids:
            raise ValueError("subject_ids must be non-empty")


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    """Deterministic plan a storage worker executes to satisfy a request.

    Carries only **hashed** subject ids, the target tables, and the SLA
    deadline. The actual DML is out of scope for this module (CTO-76 infra).
    """

    tenant_id: str
    hashed_subject_ids: tuple[str, ...]
    target_tables: tuple[str, ...]
    received_at: datetime
    sla_deadline: datetime

    @property
    def subject_count(self) -> int:
        return len(self.hashed_subject_ids)

    def is_overdue(self, now: datetime) -> bool:
        return _as_utc(now) > self.sla_deadline

    def summary(self) -> str:
        return (
            f"deletion for {self.tenant_id}: {self.subject_count} subject(s) across "
            f"{len(self.target_tables)} table(s), due {self.sla_deadline.isoformat()}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "hashed_subject_ids": list(self.hashed_subject_ids),
            "target_tables": list(self.target_tables),
            "received_at": self.received_at.isoformat(),
            "sla_deadline": self.sla_deadline.isoformat(),
            "subject_count": self.subject_count,
        }


def build_deletion_plan(
    request: DeletionRequest,
    *,
    target_tables: Iterable[str] = DELETION_TARGET_TABLES,
    sla_days: int = DEFAULT_DELETION_SLA_DAYS,
) -> DeletionPlan:
    """Turn a :class:`DeletionRequest` into a :class:`DeletionPlan`.

    Subject ids are hashed (deduplicated, order-stable) and the SLA deadline is
    ``received_at + sla_days``. Naive ``received_at`` is treated as UTC.
    """
    if sla_days <= 0:
        raise ValueError("sla_days must be positive")
    received = _as_utc(request.received_at)
    seen: set[str] = set()
    hashed: list[str] = []
    for sid in request.subject_ids:
        h = hash_subject_id(sid)
        if h not in seen:
            seen.add(h)
            hashed.append(h)
    return DeletionPlan(
        tenant_id=request.tenant_id,
        hashed_subject_ids=tuple(hashed),
        target_tables=tuple(target_tables),
        received_at=received,
        sla_deadline=received + timedelta(days=sla_days),
    )


def _as_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC; normalise aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
