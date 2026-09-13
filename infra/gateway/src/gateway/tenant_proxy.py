# SPDX-License-Identifier: Apache-2.0
"""Per-organization on/off switch for the hosted edge proxy (zero-code connect).

Off by default. Routing production LLM calls through ai-tally's server is a trust decision, so an org
admin turns it on explicitly and a key that works for the SDK is refused by the proxy until they do.
See ``db/postgres/0033_tenant_proxy_config.sql`` for the schema and why the switch rides the edge-key
feed rather than being a separate channel.

A tenant with no row reads as ``enabled=False``: the gateway never provisions a row at signup, and the
row appears only when an admin first changes the setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from gateway.config import Settings
from gateway.tenant_lookup import resolve_tenant_uuid


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    enabled: bool
    #: When it last changed. None for a tenant that has never touched the setting.
    updated_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


DEFAULT_CONFIG = ProxyConfig(enabled=False, updated_at=None)


class TenantProxyStore:
    """Postgres surface over ``tenant_proxy_config``."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> ProxyConfig:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "SELECT enabled, updated_at FROM tenant_proxy_config WHERE tenant_id = %s",
                (resolved,),
            )
            row = cur.fetchone()
        if row is None:
            return DEFAULT_CONFIG
        return ProxyConfig(enabled=bool(row[0]), updated_at=row[1])

    def set_enabled(self, tenant_id: str, enabled: object, *, updated_by: str | None = None) -> ProxyConfig:
        """Turn the proxy on or off for a tenant, and push the change into the edge-key feed.

        ``enabled`` must be a real boolean. A string "false" is truthy in Python, and coercing it would
        switch a tenant's proxy ON when the caller meant off, so anything else is refused.

        The config write and the ``api_keys.edge_updated_at`` stamp commit in ONE transaction. If they
        were separate, a proxy poll between them could cache the old value against a watermark that
        has already moved past it, and the change would never arrive.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if updated_by is not None and len(updated_by) > 128:
            updated_by = None  # audit only; never fail the toggle over an oversized id
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                INSERT INTO tenant_proxy_config (tenant_id, enabled, updated_by, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET enabled    = EXCLUDED.enabled,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = now()
                RETURNING enabled, updated_at
                """,
                (resolved, enabled, updated_by),
            )
            row = cur.fetchone()
            # Revoked keys are already gone from every proxy cache; re-emitting them would be noise.
            cur.execute(
                "UPDATE api_keys SET edge_updated_at = now() WHERE tenant_id = %s AND revoked_at IS NULL",
                (resolved,),
            )
            conn.commit()
        return ProxyConfig(enabled=bool(row[0]), updated_at=row[1])
