"""Per-tenant credentials-by-reference for the Segment / HubSpot / Pendo workers (CTO-127).

Each ingest worker needs a per-tenant credential to reach the third party (Segment source
write-key, HubSpot OAuth token, Pendo integration key). Mirroring the tradeoff documented in
:mod:`gateway.tenant_stripe` / ``0003_tenant_stripe_config.sql``, this module never stores the raw
credential — the control plane holds a *reference* (a Secret Manager / Vault / KMS handle) and a
:class:`SecretResolver` dereferences it at run time.

That indirection is what lets the invariant "credentials by reference" hold end-to-end: the worker
asks the store for a ``secret_ref``, hands it to the resolver, and only ever holds the resolved
token in a local variable for the duration of one HTTP call. In tests the resolver is a trivial
in-memory dict, so no real secret and no network is involved.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import psycopg

from gateway.config import Settings

# The connectors this table serves. Stripe is intentionally excluded — it has its own config table
# (``tenant_stripe_config``) because its webhook path needs the raw signing secret to verify HMACs.
ALLOWED_SECRET_CONNECTORS: frozenset[str] = frozenset({"segment", "hubspot", "pendo"})


@dataclass(frozen=True, slots=True)
class IntegrationSecret:
    """One ``tenant_integration_secrets`` row — a credential *reference*, never the raw key.

    ``config`` holds only non-secret connector knobs (base-urls, portal id). ``secret_ref`` is the
    handle the :class:`SecretResolver` turns into a live token.
    """

    tenant_id: str
    connector_id: str
    secret_ref: str
    config: dict[str, object] = field(default_factory=dict)
    connected_at: str = ""
    disconnected_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.disconnected_at is None


@runtime_checkable
class SecretResolver(Protocol):
    """Turns a stored ``secret_ref`` into a live credential value.

    Production wires a Secret Manager / Vault client here; :class:`EnvSecretResolver` is the
    dependency-free default. Implementations MUST raise on a missing / unreadable reference rather
    than return an empty string, and MUST NOT echo the resolved value into the exception message.
    """

    def resolve(self, secret_ref: str) -> str: ...


class EnvSecretResolver:
    """Dev / self-hosted resolver: the ``secret_ref`` names an environment variable.

    Raises :class:`KeyError` when the variable is absent — deliberately without echoing any value,
    so a resolution failure can be surfaced to ``record_run`` without leaking the credential.
    """

    def resolve(self, secret_ref: str) -> str:
        value = os.environ.get(secret_ref)
        if not value:
            raise KeyError(f"no credential resolvable for reference {secret_ref!r}")
        return value


class TenantIntegrationSecretStore:
    """Tenant-scoped CRUD over ``tenant_integration_secrets``. No cross-tenant queries."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str, connector_id: str) -> IntegrationSecret | None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, connector_id, secret_ref, config,
                       connected_at, disconnected_at
                FROM tenant_integration_secrets
                WHERE tenant_id = %s AND connector_id = %s
                """,
                (tenant_id, connector_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_secret(row)

    def upsert(
        self,
        tenant_id: str,
        connector_id: str,
        secret_ref: str,
        *,
        config: dict[str, object] | None = None,
    ) -> IntegrationSecret:
        """Insert or rotate the (tenant, connector) credential reference.

        Rotating is the same op as connecting again — one row per (tenant, connector); a rotate
        clears any prior ``disconnected_at`` so the worker resumes.
        """
        if connector_id not in ALLOWED_SECRET_CONNECTORS:
            raise ValueError(f"unknown connector_id '{connector_id}'")
        if not secret_ref:
            raise ValueError("secret_ref must be a non-empty reference")
        payload = json.dumps(config or {})
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_integration_secrets
                       (tenant_id, connector_id, secret_ref, config)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE
                  SET secret_ref = EXCLUDED.secret_ref,
                      config = EXCLUDED.config,
                      disconnected_at = NULL
                RETURNING tenant_id, connector_id, secret_ref, config,
                          connected_at, disconnected_at
                """,
                (tenant_id, connector_id, secret_ref, payload),
            )
            row = cur.fetchone()
            conn.commit()
            assert row is not None
            return _row_to_secret(row)

    def disconnect(self, tenant_id: str, connector_id: str) -> None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenant_integration_secrets
                   SET disconnected_at = now()
                 WHERE tenant_id = %s AND connector_id = %s AND disconnected_at IS NULL
                """,
                (tenant_id, connector_id),
            )
            conn.commit()


def _row_to_secret(row: tuple[object, ...]) -> IntegrationSecret:
    raw_config = row[3]
    if isinstance(raw_config, str):
        try:
            config = json.loads(raw_config)
        except json.JSONDecodeError:
            config = {}
    elif isinstance(raw_config, dict):
        config = raw_config
    else:
        config = {}
    connected = row[4]
    disconnected = row[5]
    return IntegrationSecret(
        tenant_id=str(row[0]),
        connector_id=str(row[1]),
        secret_ref=str(row[2]),
        config=config,
        connected_at=connected.isoformat() if hasattr(connected, "isoformat") else "",
        disconnected_at=(
            disconnected.isoformat() if hasattr(disconnected, "isoformat") else None
        ),
    )
