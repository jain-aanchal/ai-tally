"""Tamper-evident audit log + short-lived access tokens for privileged admin actions.

Implements CTO-75 (Access control + audit log).

Privileged admin actions (tenant create, config change, deletion, key rotation) and every
production data read must leave an immutable trail that an auditor can later trust and query.
"Trust" here means tamper-evidence: if anyone edits, reorders, or drops a record after the fact,
verification must surface it. We get that with a per-entry SHA-256 **hash chain** — each entry
commits to the previous entry's hash, so a single altered field invalidates every link after it.
The log is **append-only**: even retention (7 years) is reported, never enforced by deletion, so
the chain can always be re-verified end-to-end.

The companion concern is *who* may touch production data. We model the policy, not the infra:
production DB access is granted through **short-lived STS-style tokens** (max TTL 1h, scoped),
there are no shared standing admin credentials, and the **break-glass** path that mints an
elevated token ALWAYS forces a ``BREAK_GLASS`` audit entry — emergency access is allowed but never
silent.

Everything is pure Python over injected stores/clocks so dev and test run offline with no database.
Out of scope (other tickets): encryption/KMS (CTO-74), real STS/cloud wiring, pentest/IR (CTO-77).

Time is integer **seconds** since the Unix epoch throughout this module (audit and tokens both),
chosen over nanoseconds for readability of human-scale durations like TTLs and the 7-year window.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

GENESIS_PREV_HASH = "0" * 64
MAX_TOKEN_TTL_S = 3600  # short-lived STS-style tokens: at most one hour
SEVEN_YEARS_S = 7 * 365 * 24 * 3600  # retention window (ignores leap days — coarse on purpose)


class AuditAction(str, Enum):
    """The privileged actions worth an immutable record. ``str`` mixin keeps values JSON-safe."""

    TENANT_CREATE = "tenant_create"
    CONFIG_CHANGE = "config_change"
    DELETION = "deletion"
    KEY_ROTATION = "key_rotation"
    DATA_ACCESS = "data_access"  # "all production data reads logged"
    BREAK_GLASS = "break_glass"


def _canonical_json(obj: object) -> str:
    """Stable serialization for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One link in the hash chain. Immutable once recorded.

    ``entry_hash`` is derived from every other field (including ``prev_hash``) and is therefore
    excluded from the hashed payload — it is the output, not an input.
    """

    sequence: int
    actor: str
    action: AuditAction
    tenant_id: str
    target: str
    occurred_at_s: int
    prev_hash: str
    entry_hash: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError(f"sequence must be >= 0, got {self.sequence}")
        if not isinstance(self.action, AuditAction):
            raise ValueError(f"action must be an AuditAction, got {type(self.action).__name__}")
        if not self.actor:
            raise ValueError("actor must be non-empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")

    def canonical_payload(self) -> dict[str, object]:
        """The fields the chain commits to — everything except the derived ``entry_hash``."""
        return {
            "sequence": self.sequence,
            "actor": self.actor,
            "action": self.action.value,
            "tenant_id": self.tenant_id,
            "target": self.target,
            "occurred_at_s": self.occurred_at_s,
            "prev_hash": self.prev_hash,
            "metadata": self.metadata,
        }

    def recompute_hash(self) -> str:
        """Recompute ``entry_hash`` from the canonical payload — the verification primitive."""
        return hashlib.sha256(_canonical_json(self.canonical_payload()).encode()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        d = self.canonical_payload()
        d["entry_hash"] = self.entry_hash
        return d


@runtime_checkable
class AuditStore(Protocol):
    """Append-only persistence boundary. Implementations MUST NOT mutate or drop entries."""

    def append(self, entry: AuditEntry) -> None: ...

    def all(self) -> list[AuditEntry]:
        """Entries in insertion (sequence) order."""
        ...


class InMemoryAuditStore:
    """Default offline store — a list nobody outside this class may reorder."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(self, entry: AuditEntry) -> None:
        self._entries.append(entry)

    def all(self) -> list[AuditEntry]:
        return list(self._entries)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a full chain walk."""

    ok: bool
    entries_checked: int
    broken_at_sequence: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "broken_at_sequence": self.broken_at_sequence,
            "reason": self.reason,
        }

    def summary(self) -> str:
        if self.ok:
            return f"audit chain OK ({self.entries_checked} entries verified)"
        return (
            f"audit chain BROKEN at sequence {self.broken_at_sequence}: "
            f"{self.reason} (checked {self.entries_checked})"
        )


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """Which entries have aged past the 7-year window. Informational only — nothing is deleted."""

    now_s: int
    window_s: int
    expired_sequences: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "now_s": self.now_s,
            "window_s": self.window_s,
            "expired_sequences": list(self.expired_sequences),
        }


def _default_clock() -> int:
    import time

    return int(time.time())


class AuditLog:
    """Append-only, hash-chained audit log over an injected :class:`AuditStore`."""

    __slots__ = ("_store", "_clock")

    def __init__(
        self,
        store: AuditStore | None = None,
        *,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        self._store = store if store is not None else InMemoryAuditStore()
        self._clock = clock

    def record(
        self,
        actor: str,
        action: AuditAction,
        tenant_id: str,
        *,
        target: str = "",
        occurred_at_s: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuditEntry:
        """Append a new chained entry and return it.

        Raises ``ValueError`` on programmer misuse (empty actor/tenant, unknown action).
        """
        if not actor:
            raise ValueError("actor must be non-empty")
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(action, AuditAction):
            raise ValueError(f"action must be an AuditAction, got {type(action).__name__}")

        existing = self._store.all()
        sequence = len(existing)
        prev_hash = existing[-1].entry_hash if existing else GENESIS_PREV_HASH
        when = occurred_at_s if occurred_at_s is not None else self._clock()

        scaffold = AuditEntry(
            sequence=sequence,
            actor=actor,
            action=action,
            tenant_id=tenant_id,
            target=target,
            occurred_at_s=when,
            prev_hash=prev_hash,
            entry_hash="",
            metadata=dict(metadata) if metadata else {},
        )
        entry = AuditEntry(
            sequence=scaffold.sequence,
            actor=scaffold.actor,
            action=scaffold.action,
            tenant_id=scaffold.tenant_id,
            target=scaffold.target,
            occurred_at_s=scaffold.occurred_at_s,
            prev_hash=scaffold.prev_hash,
            entry_hash=scaffold.recompute_hash(),
            metadata=scaffold.metadata,
        )
        self._store.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        return self._store.all()

    def verify(self) -> VerificationResult:
        """Walk the chain, recomputing each link. Detects tamper, reorder, and sequence gaps.

        Defensive on the boundary: a malformed/unhashable record is reported as a broken link
        rather than allowed to crash the walk.
        """
        entries = self._store.all()
        expected_prev = GENESIS_PREV_HASH
        for index, entry in enumerate(entries):
            if entry.sequence != index:
                return VerificationResult(
                    ok=False,
                    entries_checked=index,
                    broken_at_sequence=entry.sequence,
                    reason=f"sequence gap/reorder: expected {index}, found {entry.sequence}",
                )
            if entry.prev_hash != expected_prev:
                return VerificationResult(
                    ok=False,
                    entries_checked=index,
                    broken_at_sequence=entry.sequence,
                    reason="prev_hash does not match preceding entry (reorder or insertion)",
                )
            try:
                recomputed = entry.recompute_hash()
            except (TypeError, ValueError):
                return VerificationResult(
                    ok=False,
                    entries_checked=index,
                    broken_at_sequence=entry.sequence,
                    reason="entry payload is not hashable (malformed record)",
                )
            if recomputed != entry.entry_hash:
                return VerificationResult(
                    ok=False,
                    entries_checked=index,
                    broken_at_sequence=entry.sequence,
                    reason="entry_hash mismatch (tampered field)",
                )
            expected_prev = entry.entry_hash
        return VerificationResult(ok=True, entries_checked=len(entries))

    def query(
        self,
        *,
        actor: str | None = None,
        action: AuditAction | None = None,
        tenant_id: str | None = None,
        since_s: int | None = None,
        until_s: int | None = None,
    ) -> list[AuditEntry]:
        """Auditor query: filter by actor, action, tenant, and an inclusive time range."""
        results: list[AuditEntry] = []
        for entry in self._store.all():
            if actor is not None and entry.actor != actor:
                continue
            if action is not None and entry.action != action:
                continue
            if tenant_id is not None and entry.tenant_id != tenant_id:
                continue
            if since_s is not None and entry.occurred_at_s < since_s:
                continue
            if until_s is not None and entry.occurred_at_s > until_s:
                continue
            results.append(entry)
        return results

    def retention_report(
        self,
        *,
        now_s: int | None = None,
        window_s: int = SEVEN_YEARS_S,
    ) -> RetentionReport:
        """Report entries older than the window. Never deletes — the log stays append-only."""
        now = now_s if now_s is not None else self._clock()
        cutoff = now - window_s
        expired = tuple(
            entry.sequence for entry in self._store.all() if entry.occurred_at_s < cutoff
        )
        return RetentionReport(now_s=now, window_s=window_s, expired_sequences=expired)


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A short-lived, scoped credential for production access. No standing admin accounts."""

    token_id: str
    subject: str
    scopes: frozenset[str]
    issued_at_s: int
    expires_at_s: int
    elevated: bool = False

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("subject must be non-empty")
        if self.expires_at_s <= self.issued_at_s:
            raise ValueError("expires_at_s must be after issued_at_s")

    @property
    def ttl_s(self) -> int:
        return self.expires_at_s - self.issued_at_s

    def is_expired(self, now_s: int) -> bool:
        return now_s >= self.expires_at_s

    def is_valid(self, now_s: int, *, required_scope: str | None = None) -> bool:
        """True only if unexpired and (when given) carrying the required scope."""
        if self.is_expired(now_s):
            return False
        if required_scope is not None and required_scope not in self.scopes:
            return False
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "token_id": self.token_id,
            "subject": self.subject,
            "scopes": sorted(self.scopes),
            "issued_at_s": self.issued_at_s,
            "expires_at_s": self.expires_at_s,
            "elevated": self.elevated,
        }


class TokenIssuer:
    """Mints short-lived scoped tokens, capping TTL at :data:`MAX_TOKEN_TTL_S` (1h).

    The **break-glass** path issues an elevated token for emergencies but ALWAYS writes a
    ``BREAK_GLASS`` audit entry first — emergency access is permitted, never silent.
    """

    __slots__ = ("_max_ttl_s", "_clock", "_token_id_factory")

    def __init__(
        self,
        *,
        max_ttl_s: int = MAX_TOKEN_TTL_S,
        clock: Callable[[], int] = _default_clock,
        token_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_ttl_s <= 0:
            raise ValueError(f"max_ttl_s must be > 0, got {max_ttl_s}")
        if max_ttl_s > MAX_TOKEN_TTL_S:
            raise ValueError(
                f"max_ttl_s must be <= {MAX_TOKEN_TTL_S}s (1h policy cap), got {max_ttl_s}"
            )
        self._max_ttl_s = max_ttl_s
        self._clock = clock
        self._token_id_factory = token_id_factory or (lambda: uuid.uuid4().hex)

    def issue(
        self,
        subject: str,
        scopes: set[str] | frozenset[str],
        ttl_s: int,
        *,
        elevated: bool = False,
    ) -> AccessToken:
        """Issue a scoped token. ``ValueError`` if ``ttl_s`` is non-positive or over the cap."""
        if not subject:
            raise ValueError("subject must be non-empty")
        if ttl_s <= 0:
            raise ValueError(f"ttl_s must be > 0, got {ttl_s}")
        if ttl_s > self._max_ttl_s:
            raise ValueError(
                f"ttl_s {ttl_s}s exceeds max {self._max_ttl_s}s (short-lived-token policy)"
            )
        now = self._clock()
        return AccessToken(
            token_id=self._token_id_factory(),
            subject=subject,
            scopes=frozenset(scopes),
            issued_at_s=now,
            expires_at_s=now + ttl_s,
            elevated=elevated,
        )

    def break_glass(
        self,
        subject: str,
        reason: str,
        audit_log: AuditLog,
        *,
        scopes: set[str] | frozenset[str] | None = None,
        ttl_s: int | None = None,
        tenant_id: str = "*",
    ) -> AccessToken:
        """Emergency elevated access. Records a ``BREAK_GLASS`` entry, THEN returns the token.

        ``reason`` is mandatory (the documented break-glass procedure requires justification).
        """
        if not reason:
            raise ValueError("break-glass requires a non-empty reason")
        granted = ttl_s if ttl_s is not None else self._max_ttl_s

        before = len(audit_log.entries())
        audit_log.record(
            actor=subject,
            action=AuditAction.BREAK_GLASS,
            tenant_id=tenant_id,
            target="production",
            metadata={"reason": reason, "ttl_s": granted},
        )
        # The whole point of break-glass is that it can never be silent.
        assert len(audit_log.entries()) == before + 1, "break-glass failed to write an audit entry"

        return self.issue(
            subject,
            scopes if scopes is not None else {"admin"},
            granted,
            elevated=True,
        )
