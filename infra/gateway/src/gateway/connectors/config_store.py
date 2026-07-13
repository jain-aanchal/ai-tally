"""Postgres surface for the compute connector's control-plane row (CTO-143).

Reads the per-tenant ``tenant_compute_config`` row (which provider, credential reference, tag filter)
and stamps run status back onto it. Same narrow, tenant-scoped CRUD shape as
:class:`gateway.tenant_connectors.TenantConnectorStore` and
:class:`gateway.tenant_integrations.TenantIntegrationStore`.

``record_run`` mirrors ``TenantIntegrationStore.record_run`` so egress (CTO-144) can reuse the exact
recorder contract — it just updates ``last_run_at`` / ``last_status`` on the config row.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from gateway.config import Settings
from gateway.connectors.base import ConnectorConfig

_ALLOWED_STATUSES = frozenset({"success", "partial", "failed"})


class TenantComputeConfigStore:
    """Tiny Postgres-backed reader/recorder over ``tenant_compute_config``."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def load_config(self, tenant_id: str) -> ConnectorConfig | None:
        """Return the tenant's compute connector config, or ``None`` if not configured.

        ``None`` means "compute connector not enabled for this tenant" — the connector skips it,
        which keeps the migration additive (existing tenants have zero rows).
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, cloud_provider, credentials_ref, tag_filter
                FROM tenant_compute_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        tag_filter = row[3]
        if isinstance(tag_filter, str):  # driver returned raw JSON text
            tag_filter = json.loads(tag_filter)
        return ConnectorConfig(
            tenant_id=str(row[0]),
            cloud_provider=str(row[1]),
            credentials_ref=str(row[2]),
            tag_filter=dict(tag_filter or {}),
        )

    def record_run(
        self,
        tenant_id: str,
        connector_id: str,  # noqa: ARG002 - kept for RunRecorder shape parity with egress
        status: str,
        *,
        error_message: str | None = None,  # noqa: ARG002 - not persisted on the config row (v1)
    ) -> None:
        """Stamp the outcome of one connector cycle on the tenant's config row.

        Only ``last_run_at`` / ``last_status`` are persisted for v1 — the config row is not an
        append-only run log (that's the deferred /connectors tile's concern, CTO-140). ``connector_id``
        and ``error_message`` are accepted so the signature matches the ``RunRecorder`` protocol the
        base connector (and egress) call through.
        """
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"unknown status '{status}'")
        params: tuple[Any, ...] = (status, tenant_id)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenant_compute_config
                SET last_run_at = now(), last_status = %s
                WHERE tenant_id = %s
                """,
                params,
            )
            conn.commit()
