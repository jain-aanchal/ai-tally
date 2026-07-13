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
from decimal import Decimal
from typing import Any

import psycopg

from gateway.config import Settings
from gateway.connectors.base import ConnectorConfig
from gateway.connectors.egress import EgressConfig

_ALLOWED_STATUSES = frozenset({"success", "partial", "failed"})


def _as_dict(value: Any) -> dict[str, str]:
    """Coerce a JSONB column (dict, raw JSON text, or NULL) to a plain ``dict``."""
    if isinstance(value, str):  # driver returned raw JSON text
        value = json.loads(value)
    return dict(value or {})


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
                SELECT tenant_id, cloud_provider, credentials_ref, tag_filter,
                       bq_billing_export_table, label_filter
                FROM tenant_compute_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ConnectorConfig(
            tenant_id=str(row[0]),
            cloud_provider=str(row[1]),
            credentials_ref=str(row[2]),
            tag_filter=_as_dict(row[3]),
            bq_billing_export_table=str(row[4] or ""),
            label_filter=_as_dict(row[5]),
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


class TenantEgressConfigStore:
    """Postgres reader/recorder over ``tenant_egress_config`` (CTO-144).

    Egress differs from compute in ONE way at the control plane: a tenant may configure MULTIPLE
    egress providers (Vercel + Cloudflare + AWS), so the table's PK is ``(tenant_id, egress_provider)``
    and :meth:`load_configs` returns a LIST (one :class:`EgressConfig` per provider). Running the
    :class:`gateway.connectors.egress.EgressCostConnector` once per returned config is what makes the
    providers sum without double-counting — each provider keys a distinct synthetic span id.

    ``record_run`` matches the ``RunRecorder`` protocol the base connector calls through, and is
    scoped to a single provider (the connector runs one provider at a time).
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def load_configs(self, tenant_id: str) -> list[EgressConfig]:
        """Return every egress provider configured for the tenant (empty list if none).

        An empty list means "egress connector not enabled" — the connector skips the tenant, keeping
        the migration additive.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, egress_provider, credentials_ref, resource_id, usd_per_gb
                FROM tenant_egress_config
                WHERE tenant_id = %s
                ORDER BY egress_provider
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()
        configs: list[EgressConfig] = []
        for row in rows:
            usd_per_gb = row[4]
            configs.append(
                EgressConfig(
                    tenant_id=str(row[0]),
                    cloud_provider=str(row[1]),
                    credentials_ref=str(row[2]),
                    resource_id=str(row[3] or ""),
                    usd_per_gb=Decimal(str(usd_per_gb)) if usd_per_gb is not None else None,
                )
            )
        return configs

    def load_config(self, tenant_id: str, provider: str) -> EgressConfig | None:
        """Return a single tenant+provider egress config, or ``None`` if not configured."""
        for config in self.load_configs(tenant_id):
            if config.cloud_provider == provider:
                return config
        return None

    def recorder_for(self, provider: str) -> _ProviderScopedRecorder:
        """A :class:`RunRecorder` bound to one ``(tenant, provider)`` egress row.

        The base connector runs a single provider per instance and calls ``record_run`` with its
        ``operation`` (``'egress'``), which does not identify the provider. Binding the provider at
        recorder construction keeps run-status per-provider without changing the reused base contract.
        """
        return _ProviderScopedRecorder(self, provider)

    def record_provider_run(self, tenant_id: str, provider: str, status: str) -> None:
        """Stamp ``last_run_at`` / ``last_status`` on the tenant's ``(tenant, provider)`` egress row."""
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"unknown status '{status}'")
        params: tuple[Any, ...] = (status, tenant_id, provider)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenant_egress_config
                SET last_run_at = now(), last_status = %s
                WHERE tenant_id = %s AND egress_provider = %s
                """,
                params,
            )
            conn.commit()


class _ProviderScopedRecorder:
    """Adapts :class:`TenantEgressConfigStore` to the ``RunRecorder`` protocol for ONE provider.

    The base connector calls ``record_run(tenant, connector_id, status)`` with ``connector_id`` set
    to its ``operation`` (``'egress'``); this adapter ignores that and stamps the pre-bound provider's
    row, so multi-provider tenants keep independent run status.
    """

    def __init__(self, store: TenantEgressConfigStore, provider: str) -> None:
        self._store = store
        self._provider = provider

    def record_run(
        self,
        tenant_id: str,
        connector_id: str,  # noqa: ARG002 - always 'egress'; provider is bound at construction
        status: str,
        *,
        error_message: str | None = None,  # noqa: ARG002 - not persisted on the config row (v1)
    ) -> None:
        self._store.record_provider_run(tenant_id, self._provider, status)
