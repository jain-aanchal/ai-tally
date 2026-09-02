# SPDX-License-Identifier: Apache-2.0
"""Serve a tenant's active HMAC key material for in-process hashing (Initiative 2, §3.2).

WHY this exists. The SDK's one-line ``tally.init(key)`` needs to hash account and user ids inside
the customer process so a raw id never leaves it (CLAUDE.md: identifiers by hash). To hash, it needs
the tenant's active HMAC key material and its version. This module is the gateway half of the
``GET /v1/tenant/hmac-key`` bootstrap: it resolves the caller's tenant onto ``tenants.id``, reads the
per-org HMAC key reference Initiative 1 provisioned into ``tenants.hash_salt_kek_ref``, and returns
the active key bytes (base64) plus the version parsed from that reference.

WHAT IT IS NOT. It is not a general KMS proxy. It returns exactly one tenant's one active symmetric
key, resolved through the same :class:`gateway.tenant_provisioning.KeyMaterialProvider` seam that
minted it. It never returns a KEK, never another tenant's key, never a raw provider credential. The
endpoint gates on the caller's own ingest key with a ``write``/``admin`` scope and is TLS-only, so
the exported bytes only ever reach a process already holding that tenant's ingest key (spec §3.2).

THE VERSION. The reference carries a trailing version selector (``.../v1``, see
``tenant_provisioning._LOCAL_KEK_SCHEME``). We surface it as ``key_version`` so the SDK stamps new
spans with the active version and a server-side rotation (a higher version on a later fetch) is
carried forward without orphaning historical hashes (spec §3.2, Option B rotation).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import psycopg

from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

#: Re-exported so a caller catching a store failure catches one name (matches the sibling stores).
TenantNotFound = TenantNotFoundError

#: The only algorithm this endpoint speaks. Named on the wire so the SDK need not assume it.
ALGORITHM = "HMAC-SHA256"

#: Default version when a reference carries no explicit ``v<n>`` selector. The provisioner always
#: appends one, so this only backstops a hand-written or legacy reference.
_DEFAULT_VERSION = "v1"


class HmacKeyUnavailableError(RuntimeError):
    """The tenant resolves but its active HMAC material cannot be produced.

    Honest under uncertainty: the endpoint turns this into a 404 rather than returning fabricated
    bytes. In local dev this is the normal state for a tenant whose material was minted in a prior
    process (the local key provider holds material in memory only); the SDK then degrades to
    unattributed accounts, never a raw id (spec §3.3).
    """


@dataclass(frozen=True, slots=True)
class HmacKeyMaterial:
    """One tenant's active HMAC key material and version. Sensitive: never log ``key_material_b64``."""

    tenant_id: str
    key_version: str
    key_material_b64: str
    algorithm: str = ALGORITHM

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "key_version": self.key_version,
            "key_material_b64": self.key_material_b64,
            "algorithm": self.algorithm,
        }


def _version_from_ref(ref: str) -> str:
    """Parse the trailing ``v<n>`` selector from a key reference (``local://hmac/<uuid>/v1``)."""
    tail = ref.rstrip("/").rsplit("/", 1)[-1]
    if tail.startswith("v") and tail[1:].isdigit():
        return tail
    return _DEFAULT_VERSION


class TenantHmacKeyStore:
    """Reads a tenant's active HMAC key material through the provisioning key-provider seam.

    The store shares the SAME :class:`KeyMaterialProvider` instance the provisioner mints into, so a
    tenant provisioned in this process resolves to the very bytes that were minted for it. Every
    method resolves the caller's tenant onto ``tenants.id`` first, so the SQL never crosses tenants.
    """

    def __init__(self, settings, key_provider) -> None:
        self._dsn = settings.postgres_dsn
        self._keys = key_provider

    def export_disabled(self, tenant_id: str) -> bool:
        """Per-tenant HMAC-export kill switch (spec §12 Q1).

        Wired now, default-on (returns False for every tenant), so the highest-security tenants can
        later be pinned proxy-only (no client-side hashing) by flipping this without touching the
        endpoint. The seam exists deliberately even though nothing flips it yet: the blast radius of
        exporting key material (spec §3.2) is exactly what such a flag would contain.
        """
        return False

    def active_key(self, tenant_id: str) -> HmacKeyMaterial:
        """Return the caller tenant's active HMAC key material + version.

        Raises :class:`TenantNotFoundError` when the identifier resolves to no tenant and
        :class:`HmacKeyUnavailableError` when the tenant has no usable material.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute("SELECT hash_salt_kek_ref FROM tenants WHERE id = %s", (resolved,))
            row = cur.fetchone()
        if row is None or not row[0]:
            raise HmacKeyUnavailableError(f"tenant {resolved} has no HMAC key reference")
        ref = str(row[0])
        try:
            material = self._keys.material(ref)
        except KeyError as exc:
            raise HmacKeyUnavailableError(
                f"active HMAC material for tenant {resolved} is unavailable"
            ) from exc
        return HmacKeyMaterial(
            tenant_id=resolved,
            key_version=_version_from_ref(ref),
            key_material_b64=base64.b64encode(material).decode("ascii"),
        )
