# SPDX-License-Identifier: Apache-2.0
"""Per-tenant shared-cost allocation rule (CTO-193, plan C2).

The cost-per-customer tab shows each account's DIRECT spend and, beside it, an ALLOCATED share of
the tenant's compute and egress. Compute and egress carry no account, so splitting them per
customer means picking a rule, and on current data that rule decides roughly half of every figure
on the page. This module is where the choice is persisted.

Two rules, matching ``AllocationRule`` in ``web/lib/allocation.ts`` one for one. ``pro_rata_direct``
is the default and the only one a tenant gets without a row here. See
``db/postgres/0024_tenant_allocation_config.sql`` for why the choice is per tenant rather than a
constant.

THE RULE OF THIS MODULE: absence of a row means the default, and that is a supported state rather
than missing config. :meth:`TenantAllocationStore.get` returns ``None`` for a tenant with no row and
the endpoint reports the default alongside ``configured: false``, so the dashboard can say "this is
the default, nobody chose it" rather than presenting an unchosen rule as a decision.

Reads and writes go through ``GET/POST /v1/tenant/allocation-config``. The web app never touches
Postgres directly, same rule as :mod:`gateway.tenant_account_labels`.

Writes are idempotent on a client-supplied ``change_id``, and append to
``tenant_allocation_config_changes``. Changing the rule moves every allocated number on the page at
once, so "who changed it, when, and from what" is the first question anyone asks afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Json

from gateway.config import Settings
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

#: Every storable rule, in the same order as ``ALLOCATION_RULES`` in ``web/lib/allocation.ts``.
#: Kept in lockstep with that list and with the CHECK constraint in migration 0024: a rule the
#: allocation engine cannot apply must not be storable.
ALLOCATION_RULES: tuple[str, ...] = ("pro_rata_direct", "even_split")

#: What a tenant with no row gets. Infra broadly scales with model usage, so proportional to direct
#: spend is the least wrong assumption available without a per-account infra driver.
DEFAULT_ALLOCATION_RULE = "pro_rata_direct"

#: Free text is capped: this is an operator identifier for the audit row, not a note field.
MAX_UPDATED_BY_CHARS = 200


class AllocationConfigError(ValueError):
    """Caller-facing validation error. Surfaces as HTTP 422."""


class TenantNotFound(AllocationConfigError):
    """The caller's tenant identifier matches no ``tenants`` row."""


@dataclass(frozen=True, slots=True)
class AllocationConfig:
    """One tenant's stored allocation rule."""

    allocation_rule: str
    created_at: datetime | None
    updated_at: datetime | None
    updated_by: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "allocation_rule": self.allocation_rule,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }


def normalize_rule(value: object) -> str:
    """Validate an allocation rule against :data:`ALLOCATION_RULES`.

    Rejects an unknown rule rather than falling back to the default, and that is the point. A
    silent fallback would store one rule, apply another, and leave the page naming a rule that did
    not produce its numbers, which is worse than a rejected write.
    """
    if not isinstance(value, str):
        raise AllocationConfigError("allocation_rule must be a string")
    trimmed = value.strip().lower()
    if not trimmed:
        raise AllocationConfigError("allocation_rule must be non-empty")
    if trimmed not in ALLOCATION_RULES:
        raise AllocationConfigError(
            "allocation_rule must be one of: " + ", ".join(ALLOCATION_RULES)
        )
    return trimmed


def normalize_updated_by(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AllocationConfigError("updated_by must be a string when provided")
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_UPDATED_BY_CHARS:
        raise AllocationConfigError(
            f"updated_by must be at most {MAX_UPDATED_BY_CHARS} characters"
        )
    return trimmed


def _resolve_tenant_uuid(cur: psycopg.Cursor, tenant_id: str) -> str:
    """Map the caller's tenant identifier onto ``tenants.id``.

    The rule now lives in :mod:`gateway.tenant_lookup` so every control-plane store shares one copy
    (CTO-201). This table keys on the UUID because of the foreign key, but the dashboard and local
    dev identify a tenant by NAME (``local-dev``). This wrapper only re-types the failure as this
    module's :class:`TenantNotFound`, which the endpoint above catches to return a clean 404.
    """
    try:
        return resolve_tenant_uuid(cur, tenant_id)
    except TenantNotFoundError as exc:
        raise TenantNotFound("no tenant matches the supplied identifier") from exc


def _row_to_config(row: tuple) -> AllocationConfig:
    return AllocationConfig(
        allocation_rule=str(row[0]),
        created_at=row[1],
        updated_at=row[2],
        updated_by=row[3],
    )


_SELECT = """
    SELECT allocation_rule, created_at, updated_at, updated_by
    FROM tenant_allocation_config
    WHERE tenant_id = %s
"""


class TenantAllocationStore:
    """Postgres surface over ``tenant_allocation_config`` and its audit log."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> AllocationConfig | None:
        """The tenant's stored rule, or ``None`` when they have never chosen one.

        ``None`` is not an error and not missing data. It is the overwhelmingly common case (every
        tenant on the system today), and the caller renders it as the default.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(_SELECT, (resolved,))
            row = cur.fetchone()
            return _row_to_config(row) if row else None

    def upsert(
        self,
        tenant_id: str,
        rule: str,
        *,
        change_id: str,
        updated_by: str | None = None,
    ) -> AllocationConfig:
        """Set the tenant's allocation rule. Idempotent on ``change_id``.

        On a new ``change_id``: reserve the audit row with the current config as ``before``, apply
        the upsert, then fill in ``after``. On a replay: no write at all, and the current row comes
        back unchanged. Same shape as
        :meth:`gateway.tenant_unit_economics.TenantUnitEconomicsStore.upsert`, for the same reason:
        a retried POST must not append a second audit row claiming a change that never happened.
        """
        rule = normalize_rule(rule)
        updated_by = normalize_updated_by(updated_by)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)

            cur.execute(_SELECT, (resolved,))
            existing = cur.fetchone()
            before = _row_to_config(existing) if existing else None

            cur.execute(
                """
                INSERT INTO tenant_allocation_config_changes
                    (change_id, tenant_id, actor, before, after)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    resolved,
                    updated_by,
                    Json(before.as_dict()) if before is not None else None,
                    None,
                ),
            )
            if cur.fetchone() is None:
                # Replayed change_id. The config write already happened on the first attempt, so
                # repeating it here would bump updated_at for a change nobody made.
                conn.commit()
                if before is not None:
                    return before
                cur.execute(_SELECT, (resolved,))
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(
                        "change_id reserved but config absent (out-of-band delete?)"
                    )
                return _row_to_config(row)

            cur.execute(
                """
                INSERT INTO tenant_allocation_config (tenant_id, allocation_rule, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                  SET allocation_rule = EXCLUDED.allocation_rule,
                      updated_by      = EXCLUDED.updated_by,
                      updated_at      = now()
                RETURNING allocation_rule, created_at, updated_at, updated_by
                """,
                (resolved, rule, updated_by),
            )
            row = cur.fetchone()
            assert row is not None  # RETURNING on an upsert that cannot no-op
            after = _row_to_config(row)

            cur.execute(
                """
                UPDATE tenant_allocation_config_changes
                SET after = %s
                WHERE tenant_id = %s AND change_id = %s
                """,
                (Json(after.as_dict()), resolved, change_id),
            )
            conn.commit()
            return after
