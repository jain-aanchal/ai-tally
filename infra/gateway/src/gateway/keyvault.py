"""In-memory customer provider-key vault — CTO-42 (spec §4.2 / §14.12).

These are the *customer's* outbound provider bearer keys (OpenAI/Anthropic/etc.) that transit our
proxy on their way to the upstream LLM. They are the top trust concern in the system, so this module
exists as a single, small, auditable surface that guarantees:

NEVER-LOG / NEVER-PERSIST GUARANTEE
-----------------------------------
* Raw key material lives **only in process memory**, keyed by ``(tenant_id, provider)``. It is never
  written to logs, disk, or the control-plane DB. (Mirrors ``auth.py``, where ``api_keys.key_hash``
  stores only a SHA-256 hash — keys are never stored raw.)
* The secret is wrapped in :class:`pydantic.SecretStr`. Its ``repr``/``str`` render as ``**********``,
  so the value cannot leak via accidental f-strings, logging, or exception messages.
* :class:`ProviderKey` (the container) has a redacting ``__repr__``/``__str__`` of the form
  ``ProviderKey(tenant=..., provider='openai', value='***redacted***')``.
* The raw value is reachable through exactly **one** explicit, named accessor,
  :meth:`ProviderKey.reveal`, which is intended to be called at the single point of egress: building
  the outbound provider request. No other method returns raw key material.

ROTATION (config-refresh channel; spec §4.2)
--------------------------------------------
:meth:`ProviderKeyVault.apply_refresh` performs an atomic per-tenant swap so the control plane can
rotate or revoke keys without a redeploy. :meth:`upsert`, :meth:`revoke`, and :meth:`rotate` are thin
helpers over it. A rotation takes effect immediately for subsequent lookups; in-flight requests
already holding a revealed value are out of scope.

CONTROL-PLANE PARTITION POLICY (spec §14.12)
--------------------------------------------
When the control plane is unreachable we apply an asymmetric, deliberately-conservative policy:

* **Fail-open on routing** — keep serving with the last-known-good cached key. Losing the control
  plane should not take down a customer's traffic (availability). See :func:`should_allow_routing`.
* **Fail-closed on guardrails** — a newly-added guardrail/deny must block even while partitioned. A
  safety control we *intended* to apply must never be silently dropped because we couldn't confirm it
  (safety). See :func:`should_apply_guardrail`.

The asymmetry is the whole point: availability is recoverable, a leaked/unguarded request is not.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from pydantic import SecretStr

__all__ = [
    "ProviderKey",
    "ProviderKeyVault",
    "GuardrailState",
    "should_allow_routing",
    "should_apply_guardrail",
]


@dataclass(frozen=True, slots=True)
class ProviderKey:
    """A single customer provider key held in memory.

    The raw secret is wrapped in :class:`~pydantic.SecretStr`; obtain it only via :meth:`reveal`,
    which is the sole egress accessor. ``repr``/``str`` are redacted so this object is safe to log.
    """

    tenant_id: str
    provider: str
    _secret: SecretStr

    @classmethod
    def create(cls, tenant_id: str, provider: str, value: str) -> ProviderKey:
        """Build a key from a raw string, wrapping it immediately so it is never stored bare."""
        return cls(tenant_id=tenant_id, provider=provider, _secret=SecretStr(value))

    def reveal(self) -> str:
        """Return the raw secret. **Only** call this at the point of egress (outbound request).

        This is the single named accessor for raw key material. Do not log, store, or pass its
        return value anywhere it could be persisted.
        """
        return self._secret.get_secret_value()

    def __repr__(self) -> str:
        return (
            f"ProviderKey(tenant={self.tenant_id!r}, provider={self.provider!r}, "
            "value='***redacted***')"
        )

    __str__ = __repr__


@dataclass(slots=True)
class ProviderKeyVault:
    """Thread-safe, in-memory store of provider keys keyed by ``(tenant_id, provider)``.

    Nothing here touches disk, the DB, or a logger. Mutations take a lock so refresh/rotate swaps are
    atomic with respect to lookups.
    """

    _keys: dict[tuple[str, str], ProviderKey] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, tenant_id: str, provider: str) -> ProviderKey | None:
        """Return the cached key for ``(tenant_id, provider)``, or ``None`` if absent/revoked."""
        with self._lock:
            return self._keys.get((tenant_id, provider))

    def has_key(self, tenant_id: str, provider: str) -> bool:
        """Whether a usable cached key exists — used by :func:`should_allow_routing`."""
        return self.get(tenant_id, provider) is not None

    def upsert(self, tenant_id: str, provider: str, value: str) -> ProviderKey:
        """Insert or replace a single key. Returns the stored (redacting) :class:`ProviderKey`."""
        key = ProviderKey.create(tenant_id, provider, value)
        with self._lock:
            self._keys[(tenant_id, provider)] = key
        return key

    def revoke(self, tenant_id: str, provider: str) -> None:
        """Remove a key so subsequent lookups miss. Idempotent."""
        with self._lock:
            self._keys.pop((tenant_id, provider), None)

    def rotate(self, tenant_id: str, provider: str, new_value: str) -> ProviderKey:
        """Atomically replace a tenant's key for ``provider``; effective for the next lookup."""
        return self.upsert(tenant_id, provider, new_value)

    def apply_refresh(
        self,
        tenant_id: str,
        keys: dict[str, str],
        *,
        prune: bool = True,
    ) -> None:
        """Atomically swap *all* of a tenant's provider keys to ``keys`` ({provider: raw_value}).

        This is the config-refresh entry point that lets the control plane rotate/revoke without a
        redeploy. The swap holds the lock for the whole tenant so a lookup never observes a partial
        update. When ``prune`` is true (default), providers absent from ``keys`` are revoked,
        matching the control plane's full-snapshot semantics.
        """
        # Wrap secrets before taking the lock to keep the critical section tiny.
        rebuilt = {
            provider: ProviderKey.create(tenant_id, provider, value)
            for provider, value in keys.items()
        }
        with self._lock:
            if prune:
                stale = [k for k in self._keys if k[0] == tenant_id and k[1] not in keys]
                for k in stale:
                    del self._keys[k]
            for provider, key in rebuilt.items():
                self._keys[(tenant_id, provider)] = key


class GuardrailState(Enum):
    """Intended state of a guardrail as last known from the control plane.

    ``PENDING`` means a guardrail was newly added/changed but we could not confirm it because the
    control plane is partitioned — we must fail closed on it.
    """

    DISABLED = "disabled"
    ENABLED = "enabled"
    PENDING = "pending"


def should_allow_routing(partitioned: bool, have_cached_key: bool) -> bool:
    """Fail-open on routing: serve as long as a last-known-good cached key exists.

    A control-plane partition must not take down customer traffic, so when ``partitioned`` we keep
    routing on the last-known-good cached key. Routing always requires an actual cached key to be
    present — there is nothing to serve without one, partitioned or not. The fail-*open* property is
    precisely that a partition does **not** flip a present key to a deny.
    """
    return have_cached_key


def should_apply_guardrail(partitioned: bool, guardrail_state: GuardrailState) -> bool:
    """Fail-closed on guardrails: apply (block) whenever the guardrail isn't confirmed-disabled.

    A guardrail explicitly known to be ``DISABLED`` is not applied. Anything else — ``ENABLED``, or a
    ``PENDING`` change we couldn't confirm because we're ``partitioned`` — is applied. A safety
    control we *meant* to enforce must never be dropped just because the control plane is unreachable.
    """
    if guardrail_state is GuardrailState.DISABLED:
        return False
    if guardrail_state is GuardrailState.ENABLED:
        return True
    # PENDING: apply if we can't confirm it (partitioned). If we're connected, a still-PENDING state
    # means the control plane hasn't promoted it to ENABLED yet, so don't apply.
    return partitioned
