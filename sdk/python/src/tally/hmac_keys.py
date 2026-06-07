# SPDX-License-Identifier: Apache-2.0
"""Per-tenant HMAC versioned-key scheme — Option B rotation (CTO-74 / spec §14.2, §10).

User IDs are never stored raw — they're HMAC-SHA256'd under a **per-tenant** key so a hash can't be
correlated across tenants and a leaked digest can't be reversed. Two hard requirements drive this
module:

1. **Versioned keys.** Every hash is stamped with the key version that produced it
   (``UserIdHashKeyVersion`` on the span). That stamp is what makes rotation possible without a
   flag-day rewrite of historical data.

2. **Option B rotation.** On compromise (or a periodic schedule) we *bump* the version rather than
   rewrite: a fresh key is provisioned, old hashes are retained as-is, and a cross-version edge
   (old_hash ↔ new_hash) is recorded in the identity graph (CTO-67) for each active user so
   attribution still bridges the boundary. A ~90d active-user re-hash backfill migrates the live
   population forward; cold users age out naturally.

The raw key material lives in cloud KMS/Vault in production — never in Postgres or code. That fetch
is abstracted behind :class:`KeyMaterialProvider`; :class:`InMemoryKeyMaterialProvider` derives
deterministic material from a root secret so dev/test needs no KMS. (Encrypted volumes, TLS 1.3, and
the real KMS wiring are the infra half of CTO-74 and are out of scope for this module.)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from tally.identity import IdentityGraph, IdentityType

UTC = timezone.utc

DEFAULT_INITIAL_VERSION = "v1"


def _hmac_digest(value: str, key: bytes) -> bytes:
    return _hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _hmac_hex(value: str, key: bytes) -> str:
    return _hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def next_version(version: str) -> str:
    """``v1`` -> ``v2``. Versions are ``v`` followed by a positive integer."""
    if not version.startswith("v") or not version[1:].isdigit():
        raise ValueError(f"version must look like 'v<n>', got {version!r}")
    return f"v{int(version[1:]) + 1}"


@runtime_checkable
class KeyMaterialProvider(Protocol):
    """Fetches raw HMAC key material for ``(tenant_id, key_version)``.

    Backed by KMS/Vault in prod; the returned bytes are used transiently and never persisted by the
    application (spec §14.2 — no secrets in Postgres or code).
    """

    def material(self, tenant_id: str, key_version: str) -> bytes: ...


@dataclass(slots=True)
class InMemoryKeyMaterialProvider:
    """Deterministically derives per-(tenant, version) key material from a root secret.

    Reproducible for tests and dev with no KMS. NOT for production — the root secret would itself
    need to live in KMS.
    """

    root_secret: bytes = b"dev-root-secret-not-for-prod"

    def material(self, tenant_id: str, key_version: str) -> bytes:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not key_version:
            raise ValueError("key_version must be non-empty")
        return _hmac_digest(f"{tenant_id}:{key_version}", self.root_secret)


@dataclass(frozen=True, slots=True)
class StampedHash:
    """An HMAC digest plus the key version that produced it (stamped on the span)."""

    value: str
    key_version: str


@dataclass(frozen=True, slots=True)
class RotationEdge:
    """A cross-version link recorded so attribution bridges a key rotation (old_hash ↔ new_hash)."""

    user_id_preview: str  # NOT stored; for plan inspection/tests only
    old_hash: str
    new_hash: str
    old_version: str
    new_version: str


class HmacKeyRegistry:
    """Per-tenant HMAC key set with a designated active version and full version history."""

    def __init__(self, provider: KeyMaterialProvider | None = None) -> None:
        self._provider: KeyMaterialProvider = provider or InMemoryKeyMaterialProvider()
        self._active: dict[str, str] = {}
        self._versions: dict[str, list[str]] = {}

    # --- provisioning + rotation -------------------------------------------------------------

    def provision(self, tenant_id: str, *, initial_version: str = DEFAULT_INITIAL_VERSION) -> str:
        """Establish a tenant's first key version at tenant creation. Idempotent."""
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if tenant_id not in self._active:
            self._active[tenant_id] = initial_version
            self._versions[tenant_id] = [initial_version]
        return self._active[tenant_id]

    def rotate(self, tenant_id: str) -> str:
        """Bump to a fresh key version (Option B): retain old versions, make the new one active."""
        self._require_provisioned(tenant_id)
        new = next_version(self._active[tenant_id])
        self._active[tenant_id] = new
        self._versions[tenant_id].append(new)
        return new

    def active_version(self, tenant_id: str) -> str:
        self._require_provisioned(tenant_id)
        return self._active[tenant_id]

    def versions(self, tenant_id: str) -> tuple[str, ...]:
        self._require_provisioned(tenant_id)
        return tuple(self._versions[tenant_id])

    # --- hashing -----------------------------------------------------------------------------

    def hash(self, tenant_id: str, user_id: str) -> StampedHash:
        """Hash ``user_id`` under the tenant's *active* key version, stamping that version."""
        version = self.active_version(tenant_id)
        return self.hash_with(tenant_id, user_id, version)

    def hash_with(self, tenant_id: str, user_id: str, key_version: str) -> StampedHash:
        """Hash under a specific version — used for re-hash backfill and historical verification."""
        self._require_provisioned(tenant_id)
        if key_version not in self._versions[tenant_id]:
            raise ValueError(f"unknown key_version {key_version!r} for tenant {tenant_id!r}")
        if not user_id:
            raise ValueError("user_id must be non-empty")
        key = self._provider.material(tenant_id, key_version)
        return StampedHash(value=_hmac_hex(user_id, key), key_version=key_version)

    def _require_provisioned(self, tenant_id: str) -> None:
        if tenant_id not in self._active:
            raise KeyError(f"tenant {tenant_id!r} has no provisioned HMAC key set")


def plan_rehash_backfill(
    registry: HmacKeyRegistry,
    tenant_id: str,
    user_ids: Iterable[str],
    *,
    old_version: str,
    new_version: str,
) -> tuple[RotationEdge, ...]:
    """For each active user, compute the old + new digests and emit the cross-version edge.

    The edges are what get recorded into the identity graph so a conversion attributed under the new
    key still reaches pre-rotation traces (and vice versa). De-duplicated and order-stable.
    """
    edges: list[RotationEdge] = []
    seen: set[str] = set()
    for user_id in user_ids:
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        old = registry.hash_with(tenant_id, user_id, old_version)
        new = registry.hash_with(tenant_id, user_id, new_version)
        edges.append(
            RotationEdge(
                user_id_preview=user_id,
                old_hash=old.value,
                new_hash=new.value,
                old_version=old_version,
                new_version=new_version,
            )
        )
    return tuple(edges)


def record_rotation_edges(
    graph: IdentityGraph,
    tenant_id: str,
    edges: Iterable[RotationEdge],
    *,
    observed_at: datetime,
) -> int:
    """Record cross-version edges into the identity graph. Returns the number of edges added."""
    count = 0
    for edge in edges:
        graph.bridge_key_versions(
            tenant_id,
            edge.old_hash,
            edge.new_hash,
            observed_at,
            identity_type=IdentityType.USER_ID,
            old_key_version=edge.old_version,
            new_key_version=edge.new_version,
        )
        count += 1
    return count
