# SPDX-License-Identifier: Apache-2.0
"""Per-tenant revenue source configuration (CTO-194).

The attribution view used to sum revenue with a hardcoded ``business_events.Source = 'stripe'``
filter. ``Source`` is an unconstrained ``LowCardinality(String)`` set by whichever connector
ingested the row, so a tenant on any other biller had every revenue event silently dropped.

``business_events.ValueType`` is the correct discriminator — it is a real enum
(``monetary`` / ``count`` / ``mrr`` / ``refund``) — and it is what the web reader keys off now.
This module owns the optional per-tenant NARROWING of that default: which ``Source`` values count
as revenue, and whether recurring ``mrr`` amounts are summed alongside one-off ``monetary`` ones.

A tenant with no row gets the defaults (every source counts; monetary + mrr count; refunds net
off), so the migration cannot break an existing tenant.

Reads/writes go through ``GET/POST /v1/tenant/revenue-sources/config`` — the web app never touches
Postgres directly (same rule as :mod:`gateway.tenant_unit_economics`). Every upsert appends a row to
``tenant_revenue_source_config_changes`` keyed by a client-supplied ``change_id`` UUID, so a retried
request is idempotent: both the config write and the audit row are no-ops on replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Json

from gateway.config import Settings
from gateway.tenant_lookup import resolve_tenant_uuid


class RevenueSourceConfigError(ValueError):
    """Caller-facing validation error — surfaces as HTTP 422 in the gateway."""


@dataclass(frozen=True, slots=True)
class RevenueSourceConfig:
    """One (tenant) row of revenue source configuration."""

    # None means "every business_events.Source counts" — the default, and NOT the same as an empty
    # list, which is rejected on the way in.
    revenue_sources: tuple[str, ...] | None
    include_mrr: bool
    created_at: datetime | None
    updated_at: datetime | None
    updated_by: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "revenue_sources": list(self.revenue_sources) if self.revenue_sources else None,
            "include_mrr": self.include_mrr,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }


def _normalize_sources(v: object) -> tuple[str, ...] | None:
    """Validate + canonicalize the source list.

    ``None`` (or a missing key) means "every source counts". Sources are lowercased and de-duped
    because ``business_events.Source`` is compared case-insensitively by the reader — the connector
    ids on the /connectors page are lowercase, but hand-written config should not have to be.
    """
    if v is None:
        return None
    if isinstance(v, str) or not isinstance(v, (list, tuple)):
        raise RevenueSourceConfigError("revenue_sources must be a list of strings or null")
    out: list[str] = []
    for item in v:
        if not isinstance(item, str):
            raise RevenueSourceConfigError("revenue_sources entries must be strings")
        s = item.strip().lower()
        if not s:
            raise RevenueSourceConfigError("revenue_sources entries must be non-empty")
        if s not in out:
            out.append(s)
    if not out:
        # "No source counts as revenue" is indistinguishable from a misconfiguration and would
        # blank the dashboard — the exact failure mode this config exists to fix. Send null to mean
        # "all sources" instead.
        raise RevenueSourceConfigError(
            "revenue_sources must name at least one source, or be null for all sources"
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class RevenueSourceConfigInput:
    revenue_sources: tuple[str, ...] | None
    include_mrr: bool
    updated_by: str | None

    @classmethod
    def from_json(cls, body: object) -> "RevenueSourceConfigInput":
        if not isinstance(body, dict):
            raise RevenueSourceConfigError("body must be a JSON object")
        updated_by = body.get("updated_by")
        if updated_by is not None and not isinstance(updated_by, str):
            raise RevenueSourceConfigError("updated_by must be a string when provided")
        include_mrr = body.get("include_mrr", True)
        if not isinstance(include_mrr, bool):
            raise RevenueSourceConfigError("include_mrr must be a boolean")
        return cls(
            revenue_sources=_normalize_sources(body.get("revenue_sources")),
            include_mrr=include_mrr,
            updated_by=updated_by,
        )


def _row_to_config(row: tuple) -> RevenueSourceConfig:
    sources = tuple(row[0]) if row[0] else None
    return RevenueSourceConfig(
        revenue_sources=sources,
        include_mrr=bool(row[1]),
        created_at=row[2],
        updated_at=row[3],
        updated_by=row[4],
    )


_SELECT = """
    SELECT revenue_sources, include_mrr, created_at, updated_at, updated_by
    FROM tenant_revenue_source_config
    WHERE tenant_id = %s
"""


class TenantRevenueSourceStore:
    """Postgres surface over ``tenant_revenue_source_config`` + its audit log.

    ``get`` returns ``None`` for a tenant with no row; the web reader then applies the defaults
    (all sources, monetary + mrr, refunds net off). ``upsert`` is idempotent on ``change_id``.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> RevenueSourceConfig | None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(_SELECT, (tenant_id,))
            row = cur.fetchone()
            return _row_to_config(row) if row else None

    def upsert(
        self,
        tenant_id: str,
        config: RevenueSourceConfigInput,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> RevenueSourceConfig:
        """Upsert the tenant's revenue source config. Idempotent on ``change_id``.

        On a new change_id: capture the current row as ``before`` (NULL if absent), apply the
        upsert, append an audit row with both before/after JSON. On a replayed change_id: no config
        write — return the existing row unchanged.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(_SELECT, (tenant_id,))
            existing_row = cur.fetchone()
            before = _row_to_config(existing_row) if existing_row else None

            cur.execute(
                """
                INSERT INTO tenant_revenue_source_config_changes
                    (change_id, tenant_id, actor, before, after)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    tenant_id,
                    actor or config.updated_by,
                    Json(before.as_dict()) if before is not None else None,
                    None,
                ),
            )
            reserved = cur.fetchone()
            if reserved is None:
                # change_id already applied — replay is a no-op. Return the current row.
                conn.commit()
                if before is not None:
                    return before
                cur.execute(_SELECT, (tenant_id,))
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("change_id reserved but config absent — out-of-band delete?")
                return _row_to_config(row)

            cur.execute(
                """
                INSERT INTO tenant_revenue_source_config
                    (tenant_id, revenue_sources, include_mrr, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                  SET revenue_sources = EXCLUDED.revenue_sources,
                      include_mrr     = EXCLUDED.include_mrr,
                      updated_by      = EXCLUDED.updated_by,
                      updated_at      = now()
                RETURNING revenue_sources, include_mrr, created_at, updated_at, updated_by
                """,
                (
                    tenant_id,
                    list(config.revenue_sources) if config.revenue_sources else None,
                    config.include_mrr,
                    config.updated_by,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            after = _row_to_config(row)

            cur.execute(
                """
                UPDATE tenant_revenue_source_config_changes
                SET after = %s
                WHERE tenant_id = %s AND change_id = %s
                """,
                (Json(after.as_dict()), tenant_id, change_id),
            )
            conn.commit()
            return after
