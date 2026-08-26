"""Per-tenant LTV/CAC band thresholds for the unit-economics view (CTO-126).

The dashboard's ``ltvCacBand`` / ``paybackBand`` classifiers colored the LTV:CAC ratio and payback
months green/yellow/red using hardcoded B2B-SaaS defaults ("tenant-configurable in v2"). This module
is v2: one row per tenant in ``tenant_unit_economics_config`` overrides the four cutoffs. A tenant
with no row keeps the hardcoded defaults — the web classify helpers apply the tenant's overrides ON
TOP of the defaults, so the defaults are always the fallback.

Reads/writes go through ``GET/POST /v1/tenant/unit-economics/config`` — the web app never touches
Postgres directly (same rule as :mod:`gateway.tenant_guardrails` and :mod:`gateway.tenant_cac`).
Every upsert appends a row to ``tenant_unit_economics_config_changes`` keyed by a client-supplied
``change_id`` UUID, so a retried request is idempotent: both the config write and the audit row are
no-ops on replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Json

from gateway.config import Settings
from gateway.tenant_lookup import resolve_tenant_uuid


class UnitEconomicsConfigError(ValueError):
    """Caller-facing validation error — surfaces as HTTP 422 in the gateway."""


@dataclass(frozen=True, slots=True)
class UnitEconomicsConfig:
    """One (tenant) row of band thresholds."""

    ltv_cac_green_threshold: float
    ltv_cac_yellow_threshold: float
    payback_months_green: float
    payback_months_yellow: float
    created_at: datetime | None
    updated_at: datetime | None
    updated_by: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ltv_cac_green_threshold": self.ltv_cac_green_threshold,
            "ltv_cac_yellow_threshold": self.ltv_cac_yellow_threshold,
            "payback_months_green": self.payback_months_green,
            "payback_months_yellow": self.payback_months_yellow,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }


def _as_number(v: object, field: str) -> float:
    if isinstance(v, bool):
        raise UnitEconomicsConfigError(f"{field} must be a number")
    if isinstance(v, (int, float)):
        result = float(v)
    elif isinstance(v, str):
        try:
            result = float(v)
        except ValueError as exc:
            raise UnitEconomicsConfigError(f"{field} must be a number, got {v!r}") from exc
    else:
        raise UnitEconomicsConfigError(f"{field} must be a number")
    if result < 0:
        raise UnitEconomicsConfigError(f"{field} must be >= 0")
    return result


@dataclass(frozen=True, slots=True)
class UnitEconomicsConfigInput:
    ltv_cac_green_threshold: float
    ltv_cac_yellow_threshold: float
    payback_months_green: float
    payback_months_yellow: float
    updated_by: str | None

    @classmethod
    def from_json(cls, body: object) -> "UnitEconomicsConfigInput":
        if not isinstance(body, dict):
            raise UnitEconomicsConfigError("body must be a JSON object")
        updated_by = body.get("updated_by")
        if updated_by is not None and not isinstance(updated_by, str):
            raise UnitEconomicsConfigError("updated_by must be a string when provided")
        inst = cls(
            ltv_cac_green_threshold=_as_number(
                body.get("ltv_cac_green_threshold"), "ltv_cac_green_threshold"
            ),
            ltv_cac_yellow_threshold=_as_number(
                body.get("ltv_cac_yellow_threshold"), "ltv_cac_yellow_threshold"
            ),
            payback_months_green=_as_number(
                body.get("payback_months_green"), "payback_months_green"
            ),
            payback_months_yellow=_as_number(
                body.get("payback_months_yellow"), "payback_months_yellow"
            ),
            updated_by=updated_by,
        )
        inst.sanity_check()
        return inst

    def sanity_check(self) -> None:
        # Higher LTV:CAC is healthier; lower payback is healthier. Reject inverted bands so the
        # dashboard can never be told green is a worse ratio than yellow.
        if self.ltv_cac_green_threshold < self.ltv_cac_yellow_threshold:
            raise UnitEconomicsConfigError(
                "ltv_cac_green_threshold must be >= ltv_cac_yellow_threshold "
                f"(got green={self.ltv_cac_green_threshold}, "
                f"yellow={self.ltv_cac_yellow_threshold})"
            )
        if self.payback_months_green > self.payback_months_yellow:
            raise UnitEconomicsConfigError(
                "payback_months_green must be <= payback_months_yellow "
                f"(got green={self.payback_months_green}, "
                f"yellow={self.payback_months_yellow})"
            )


def _row_to_config(row: tuple) -> UnitEconomicsConfig:
    return UnitEconomicsConfig(
        ltv_cac_green_threshold=float(row[0]),
        ltv_cac_yellow_threshold=float(row[1]),
        payback_months_green=float(row[2]),
        payback_months_yellow=float(row[3]),
        created_at=row[4],
        updated_at=row[5],
        updated_by=row[6],
    )


class TenantUnitEconomicsStore:
    """Postgres surface over ``tenant_unit_economics_config`` + its audit log.

    ``get`` returns ``None`` for a tenant with no row (the web classify helpers then fall back to
    the hardcoded defaults). ``upsert`` is idempotent on the client-supplied ``change_id``.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> UnitEconomicsConfig | None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: tenant_unit_economics_config keys on the tenants.id UUID, but the dashboard
            # identifies a tenant by NAME. Fold the name onto the UUID so a name-based caller does
            # not 500.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT ltv_cac_green_threshold, ltv_cac_yellow_threshold,
                       payback_months_green, payback_months_yellow,
                       created_at, updated_at, updated_by
                FROM tenant_unit_economics_config
                WHERE tenant_id = %s
                """,
                (resolved,),
            )
            row = cur.fetchone()
            return _row_to_config(row) if row else None

    def upsert(
        self,
        tenant_id: str,
        config: UnitEconomicsConfigInput,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> UnitEconomicsConfig:
        """Upsert the tenant's thresholds. Idempotent on ``change_id``.

        On a new change_id: capture the current row as ``before`` (NULL if absent), apply the
        upsert, append an audit row with both before/after JSON. On a replayed change_id: no config
        write — return the existing row unchanged.
        """
        config.sanity_check()
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: resolve a name-based tenant id onto the UUID FK once, and use it for the
            # config row and the audit rows alike so both key on the same tenant.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT ltv_cac_green_threshold, ltv_cac_yellow_threshold,
                       payback_months_green, payback_months_yellow,
                       created_at, updated_at, updated_by
                FROM tenant_unit_economics_config
                WHERE tenant_id = %s
                """,
                (resolved,),
            )
            existing_row = cur.fetchone()
            before = _row_to_config(existing_row) if existing_row else None

            cur.execute(
                """
                INSERT INTO tenant_unit_economics_config_changes
                    (change_id, tenant_id, actor, before, after)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    resolved,
                    actor or config.updated_by,
                    Json(before.as_dict()) if before is not None else None,
                    None,
                ),
            )
            reserved = cur.fetchone()
            if reserved is None:
                # change_id already applied — replay is a no-op. Return the current row.
                conn.commit()
                current = before
                if current is None:
                    cur.execute(
                        """
                        SELECT ltv_cac_green_threshold, ltv_cac_yellow_threshold,
                               payback_months_green, payback_months_yellow,
                               created_at, updated_at, updated_by
                        FROM tenant_unit_economics_config
                        WHERE tenant_id = %s
                        """,
                        (resolved,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "change_id reserved but config absent — out-of-band delete?"
                        )
                    return _row_to_config(row)
                return current

            cur.execute(
                """
                INSERT INTO tenant_unit_economics_config
                    (tenant_id, ltv_cac_green_threshold, ltv_cac_yellow_threshold,
                     payback_months_green, payback_months_yellow, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                  SET ltv_cac_green_threshold  = EXCLUDED.ltv_cac_green_threshold,
                      ltv_cac_yellow_threshold = EXCLUDED.ltv_cac_yellow_threshold,
                      payback_months_green     = EXCLUDED.payback_months_green,
                      payback_months_yellow    = EXCLUDED.payback_months_yellow,
                      updated_by               = EXCLUDED.updated_by,
                      updated_at               = now()
                RETURNING ltv_cac_green_threshold, ltv_cac_yellow_threshold,
                          payback_months_green, payback_months_yellow,
                          created_at, updated_at, updated_by
                """,
                (
                    resolved,
                    config.ltv_cac_green_threshold,
                    config.ltv_cac_yellow_threshold,
                    config.payback_months_green,
                    config.payback_months_yellow,
                    config.updated_by,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            after = _row_to_config(row)

            cur.execute(
                """
                UPDATE tenant_unit_economics_config_changes
                SET after = %s
                WHERE tenant_id = %s AND change_id = %s
                """,
                (Json(after.as_dict()), resolved, change_id),
            )
            conn.commit()
            return after
