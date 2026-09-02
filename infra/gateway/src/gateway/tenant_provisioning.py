# SPDX-License-Identifier: Apache-2.0
"""Provision an ai-tally tenant from a verified Clerk ``organization.created`` event (Initiative 1).

WHY this exists. Until now ai-tally was single-tenant: the one ``local-dev`` row was created by the
seed script. Initiative 1 makes Clerk the system of record for identity, and every new Clerk
organization must become a tenant with its OWN per-org HMAC key set so user and account hashes
cannot be joined across tenants. This module is the gateway half of that: the web ``/api/webhooks/
clerk`` route verifies the svix signature (the gateway is private and never sees Clerk directly),
then forwards the verified event to ``POST /v1/tenant/provision``, which lands here.

THE TWO INVARIANTS THIS MODULE HOLDS.

* **Idempotent and race-safe.** Clerk retries webhooks, and two deliveries can race. Provision is
  safe to call any number of times for one org: a redelivery returns the existing tenant and mints
  no new key material, and two concurrent first-deliveries settle on one tenant via
  ``INSERT ... ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS NOT NULL DO NOTHING RETURNING`` (the
  arbiter repeats the partial-index predicate so Postgres infers ``uq_tenants_clerk_org_id``).

* **No raw secret, no orphaned key.** The per-org HMAC key set is stored ONLY as a reference in
  ``tenants.hash_salt_kek_ref``, honoring its ``no_raw_secret`` CHECK (not ``sk-%``, length < 512).
  A tenant is never created without a usable reference (a mint failure fails the provision), and the
  loser of a race deletes the key set it minted so no orphaned material is left behind.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import psycopg

from gateway.config import Settings

#: Data-residency region stamped on a provisioned tenant. Initiative 1 does no residency routing on
#: ``region`` (§1 non-goals); the column stays and defaults here, matching how the seed defaults it.
DEFAULT_REGION = "auto"

#: Scheme for the local-dev HMAC key reference. It must satisfy the ``no_raw_secret`` CHECK on
#: ``tenants.hash_salt_kek_ref`` (not ``sk-%``, length < 512). The trailing ``/v1`` leaves room for a
#: version selector so a future rotation does not orphan historical hashes (§4.1, "Versioning").
_LOCAL_KEK_SCHEME = "local"


class ProvisionError(ValueError):
    """Caller-facing validation error on the provision request. Surfaces as HTTP 422."""


@runtime_checkable
class KeyMaterialProvider(Protocol):
    """Mints and deletes a per-org HMAC key set, returning only an opaque reference.

    Production backs this with Secret Manager / KMS and returns its resource reference; local dev
    uses :class:`LocalDevKeyProvider`. The application never persists raw key material to Postgres:
    only the reference is stored, in ``tenants.hash_salt_kek_ref``.

    ``material`` resolves a reference back to the active key bytes. It is the seam the Initiative 2
    HMAC bootstrap (``GET /v1/tenant/hmac-key``, spec §3.2) reads through so the SDK can hash account
    and user ids in the customer process. In prod that is a KMS/Secret Manager fetch; here it is the
    in-process map below. It returns one tenant's active symmetric key only, never a KEK and never
    another tenant's material.
    """

    def mint(self) -> str: ...

    def delete(self, ref: str) -> None: ...

    def material(self, ref: str) -> bytes: ...


@dataclass
class LocalDevKeyProvider:
    """Local-dev HMAC key provider: no cloud KMS, no cloud dependency for ``make up``.

    Generates a 256-bit random key set from a CSPRNG, holds the material in-process keyed by its
    reference, and returns a reference string that satisfies the ``no_raw_secret`` CHECK. ``delete``
    removes the material so a lost provision race cleans up its orphaned key set. This is the local
    analog of Secret Manager / KMS; the raw bytes never touch Postgres or a log.
    """

    _material: dict[str, bytes] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(self) -> str:
        material = secrets.token_bytes(32)  # 256-bit key set from a CSPRNG (§4.1)
        ref = f"{_LOCAL_KEK_SCHEME}://hmac/{uuid.uuid4()}/v1"
        with self._lock:
            self._material[ref] = material
        return ref

    def delete(self, ref: str) -> None:
        with self._lock:
            self._material.pop(ref, None)

    def has(self, ref: str) -> bool:
        """Whether a reference still resolves to material. For orphan-cleanup tests."""
        with self._lock:
            return ref in self._material

    def material(self, ref: str) -> bytes:
        """Return the active key bytes for ``ref``, or raise ``KeyError`` when it is not held.

        The bytes are the tenant's own active HMAC key set. They are handed only to the HMAC
        bootstrap endpoint under the tenant's own ingest key (spec §3.2) and are never logged. A ref
        this process never minted (for example the seeded ``local-dev`` key, or any tenant minted in
        a prior process) is a miss, not a fabricated key: the endpoint turns that into an honest
        "material unavailable" rather than returning bytes that would hash to nothing real.
        """
        with self._lock:
            material = self._material.get(ref)
        if material is None:
            raise KeyError(f"no key material held for reference {ref!r}")
        return material


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """The outcome of a provision call."""

    tenant_id: str
    plan: str
    #: True when this call inserted the tenant; False on a redelivery or a lost race (idempotent).
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {"tenant_id": self.tenant_id, "plan": self.plan, "created": self.created}


def _clean(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProvisionError(f"{field_name} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ProvisionError(f"{field_name} must be non-empty")
    return trimmed


class TenantProvisioner:
    """Turns a Clerk org into a tenant, idempotently, with its own HMAC key reference.

    See the module docstring for the two invariants. The store owns the DSN and the key provider so
    a test can inject a fake provider and assert orphan cleanup without a real Secret Manager.
    """

    def __init__(
        self, settings: Settings, key_provider: KeyMaterialProvider | None = None
    ) -> None:
        self._dsn = settings.postgres_dsn
        self._keys: KeyMaterialProvider = key_provider or LocalDevKeyProvider()

    def provision(
        self, *, clerk_org_id: object, name: object, region: str = DEFAULT_REGION
    ) -> ProvisionResult:
        clerk_org_id = _clean(clerk_org_id, field_name="clerk_org_id")
        name = _clean(name, field_name="name")

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # Fast path: an existing mapping means a redelivery. Return it and mint NOTHING, so a
            # retried webhook never rolls the tenant's HMAC key set.
            cur.execute(
                "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
            )
            row = cur.fetchone()
            if row is not None:
                return ProvisionResult(str(row[0]), str(row[1]), created=False)

            # Miss: mint the per-org HMAC key set BEFORE inserting. A mint failure raises and no
            # tenant row is written, so a tenant that cannot hash is never created (§4.1).
            kek_ref = self._keys.mint()
            try:
                # Ensure the free plan tier exists before usage_limits references it. The seed also
                # inserts it, but provision can run first on a fresh volume.
                cur.execute(
                    """
                    INSERT INTO plan_tiers (name, max_traces_per_month, max_features, price_micro_usd)
                    VALUES ('free', 1000000, 10, 0)
                    ON CONFLICT (name) DO NOTHING
                    """
                )
                # Race-safe insert: two concurrent first-deliveries both miss the SELECT above, and
                # the partial-unique arbiter lets exactly one win. The predicate is repeated so
                # Postgres infers uq_tenants_clerk_org_id.
                cur.execute(
                    """
                    INSERT INTO tenants (name, region, plan, hash_salt_kek_ref, clerk_org_id)
                    VALUES (%s, %s, 'free', %s, %s)
                    ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS NOT NULL
                    DO NOTHING
                    RETURNING id
                    """,
                    (name, region, kek_ref, clerk_org_id),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    tenant_id = str(inserted[0])
                    cur.execute(
                        """
                        INSERT INTO usage_limits (tenant_id, plan) VALUES (%s, 'free')
                        ON CONFLICT (tenant_id) DO NOTHING
                        """,
                        (tenant_id,),
                    )
                    conn.commit()
                    return ProvisionResult(tenant_id, "free", created=True)

                # Lost the race: a concurrent delivery won. Adopt its tenant and delete the key set
                # we just minted, so no orphaned material survives.
                conn.rollback()
                cur.execute(
                    "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
                )
                winner = cur.fetchone()
                self._keys.delete(kek_ref)
                if winner is None:
                    # Extremely unlikely: the conflicting row vanished between the failed insert and
                    # this read. Surface it rather than fabricate a tenant id.
                    raise ProvisionError(
                        "provision lost a race but the winning tenant could not be read"
                    )
                return ProvisionResult(str(winner[0]), str(winner[1]), created=False)
            except Exception:
                # Any failure after minting must not leak the key set.
                conn.rollback()
                self._keys.delete(kek_ref)
                raise

    def tenant_for_clerk_org(self, clerk_org_id: object) -> ProvisionResult | None:
        """Resolve a Clerk org id to ``{tenant_id, plan}`` without provisioning. ``None`` if absent."""
        clerk_org_id = _clean(clerk_org_id, field_name="org_id")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ProvisionResult(str(row[0]), str(row[1]), created=False)
