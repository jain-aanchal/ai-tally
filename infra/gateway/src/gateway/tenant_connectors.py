"""Per-tenant cost-layer connector declarations (CTO-107).

The dashboard "Partial data" banner used to fire whenever any cost layer reported zero. With only
the LLM connector wired today, the banner was permanent — useless as a signal. We fix that by
declaring per-tenant which connectors are *enabled*; the banner now fires only when an enabled
connector goes silent. A layer that was never enabled doesn't contribute to partiality.

This module is the small Postgres surface the gateway exposes to the dashboard. Reads and writes
both go through ``GET/POST /v1/tenant/connectors`` — the web app never touches Postgres directly.
The row itself is the audit trail: ``enabled_at`` / ``disabled_at`` / ``notes`` are kept around so
toggles never delete history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg

from gateway.config import Settings

# The cost-layer enum mirrors the same six layers the web app and ClickHouse use.
Layer = Literal["llm", "vector", "tools", "compute", "embeddings", "egress"]
ALLOWED_LAYERS: frozenset[str] = frozenset(
    {"llm", "vector", "tools", "compute", "embeddings", "egress"}
)


@dataclass(frozen=True, slots=True)
class ConnectorDeclaration:
    """One (tenant, layer) row — enabled when ``disabled_at is None``."""

    layer: str
    enabled: bool
    enabled_at: str
    disabled_at: str | None
    notes: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "enabled": self.enabled,
            "enabled_at": self.enabled_at,
            "disabled_at": self.disabled_at,
            "notes": self.notes,
        }


class TenantConnectorStore:
    """Tiny Postgres-backed CRUD over ``tenant_connectors``.

    Kept narrow on purpose: every method takes the tenant_id resolved by upstream auth, so the SQL
    can't accidentally cross tenants.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[ConnectorDeclaration]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT layer, enabled_at, disabled_at, notes
                FROM tenant_connectors
                WHERE tenant_id = %s
                ORDER BY layer
                """,
                (tenant_id,),
            )
            return [
                ConnectorDeclaration(
                    layer=str(row[0]),
                    enabled=row[2] is None,
                    enabled_at=row[1].isoformat() if row[1] is not None else "",
                    disabled_at=row[2].isoformat() if row[2] is not None else None,
                    notes=row[3],
                )
                for row in cur.fetchall()
            ]

    def set(
        self,
        tenant_id: str,
        layer: str,
        *,
        enabled: bool,
        notes: str | None = None,
    ) -> ConnectorDeclaration:
        """Enable or disable one layer for a tenant. Idempotent.

        Enabling re-uses the existing row (clears ``disabled_at``, keeps the original ``enabled_at``)
        so the row remains a single audit-friendly record of intent. Disabling stamps ``disabled_at``
        with ``now()`` — we never delete, so the history is intact.
        """
        if layer not in ALLOWED_LAYERS:
            raise ValueError(f"unknown layer '{layer}'")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            if enabled:
                cur.execute(
                    """
                    INSERT INTO tenant_connectors (tenant_id, layer, notes)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, layer) DO UPDATE
                      SET disabled_at = NULL,
                          notes = COALESCE(EXCLUDED.notes, tenant_connectors.notes)
                    RETURNING layer, enabled_at, disabled_at, notes
                    """,
                    (tenant_id, layer, notes),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO tenant_connectors (tenant_id, layer, disabled_at, notes)
                    VALUES (%s, %s, now(), %s)
                    ON CONFLICT (tenant_id, layer) DO UPDATE
                      SET disabled_at = now(),
                          notes = COALESCE(EXCLUDED.notes, tenant_connectors.notes)
                    RETURNING layer, enabled_at, disabled_at, notes
                    """,
                    (tenant_id, layer, notes),
                )
            row = cur.fetchone()
            conn.commit()
            assert row is not None
            return ConnectorDeclaration(
                layer=str(row[0]),
                enabled=row[2] is None,
                enabled_at=row[1].isoformat() if row[1] is not None else "",
                disabled_at=row[2].isoformat() if row[2] is not None else None,
                notes=row[3],
            )
