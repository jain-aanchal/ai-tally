"""Per-tenant guardrail control-plane (CTO-116).

Companion to :mod:`gateway.tenant_connectors`. Each row in ``tenant_guardrails`` is one rule scoped
to one tenant; the SDK polls the gateway on its config-refresh window and enforces matching rules
in-process. Every upsert appends a row to ``tenant_guardrail_changes``, keyed by a client-supplied
``change_id`` UUID so a retried request is idempotent — both the rule write and the audit row are
no-ops on replay.

Reads and writes both go through ``GET/POST /v1/tenant/guardrails`` — the web app never touches
Postgres directly. The audit log is exposed via ``GET /v1/tenant/guardrails/audit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Json

from gateway.config import Settings

ALLOWED_KINDS: frozenset[str] = frozenset(
    {"pii_gate", "cost_cap", "loop_limit", "model_deprecation"}
)
ALLOWED_STATES: frozenset[str] = frozenset({"enabled", "shadow", "disabled"})


@dataclass(frozen=True, slots=True)
class GuardrailRule:
    """One (tenant, rule_id) row."""

    rule_id: str
    kind: str
    params: dict[str, Any]
    state: str
    created_at: str
    updated_at: str
    created_by: str | None
    notes: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "params": self.params,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class GuardrailChange:
    """One audit row — before/after JSON snapshots of the rule around a change."""

    change_id: str
    rule_id: str
    actor: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    changed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "rule_id": self.rule_id,
            "actor": self.actor,
            "before": self.before,
            "after": self.after,
            "changed_at": self.changed_at,
        }


def _row_to_rule(row: tuple) -> GuardrailRule:
    return GuardrailRule(
        rule_id=str(row[0]),
        kind=str(row[1]),
        params=row[2] if isinstance(row[2], dict) else dict(row[2] or {}),
        state=str(row[3]),
        created_at=row[4].isoformat() if row[4] is not None else "",
        updated_at=row[5].isoformat() if row[5] is not None else "",
        created_by=row[6],
        notes=row[7],
    )


class TenantGuardrailStore:
    """Tiny Postgres-backed CRUD over ``tenant_guardrails`` + audit log.

    Every method takes the ``tenant_id`` resolved by upstream auth so the SQL never crosses tenants.
    Upserts are idempotent on the client-supplied ``change_id`` — the second call with the same id
    is a no-op and returns the existing rule unchanged.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[GuardrailRule]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT rule_id, kind, params, state, created_at, updated_at, created_by, notes
                FROM tenant_guardrails
                WHERE tenant_id = %s
                ORDER BY rule_id
                """,
                (tenant_id,),
            )
            return [_row_to_rule(row) for row in cur.fetchall()]

    def upsert(
        self,
        tenant_id: str,
        rule_id: str,
        *,
        kind: str,
        params: dict[str, Any],
        state: str,
        change_id: str,
        actor: str | None = None,
        notes: str | None = None,
    ) -> GuardrailRule:
        """Upsert a rule. Idempotent on ``change_id``.

        On a new change_id: capture the current row as ``before`` (NULL if absent), apply the
        upsert, then append an audit row with both before/after JSON. On a replayed change_id:
        no SQL writes — just return the existing rule.
        """
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unknown kind '{kind}'")
        if state not in ALLOWED_STATES:
            raise ValueError(f"unknown state '{state}'")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT rule_id, kind, params, state, created_at, updated_at, created_by, notes
                FROM tenant_guardrails
                WHERE tenant_id = %s AND rule_id = %s
                """,
                (tenant_id, rule_id),
            )
            existing_row = cur.fetchone()
            before_rule = _row_to_rule(existing_row) if existing_row else None

            cur.execute(
                """
                INSERT INTO tenant_guardrail_changes
                    (change_id, tenant_id, rule_id, actor, before, after)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    tenant_id,
                    rule_id,
                    actor,
                    Json(before_rule.as_dict()) if before_rule is not None else None,
                    None,
                ),
            )
            reserved = cur.fetchone()
            if reserved is None:
                conn.commit()
                if before_rule is None:
                    cur.execute(
                        """
                        SELECT rule_id, kind, params, state, created_at, updated_at,
                               created_by, notes
                        FROM tenant_guardrails
                        WHERE tenant_id = %s AND rule_id = %s
                        """,
                        (tenant_id, rule_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "change_id reserved but rule absent — out-of-band delete?"
                        )
                    return _row_to_rule(row)
                return before_rule

            cur.execute(
                """
                INSERT INTO tenant_guardrails
                    (tenant_id, rule_id, kind, params, state, created_by, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, rule_id) DO UPDATE
                  SET kind = EXCLUDED.kind,
                      params = EXCLUDED.params,
                      state = EXCLUDED.state,
                      notes = COALESCE(EXCLUDED.notes, tenant_guardrails.notes),
                      updated_at = now()
                RETURNING rule_id, kind, params, state, created_at, updated_at, created_by, notes
                """,
                (tenant_id, rule_id, kind, Json(params), state, actor, notes),
            )
            row = cur.fetchone()
            assert row is not None
            after_rule = _row_to_rule(row)

            cur.execute(
                """
                UPDATE tenant_guardrail_changes
                SET after = %s
                WHERE tenant_id = %s AND change_id = %s
                """,
                (Json(after_rule.as_dict()), tenant_id, change_id),
            )
            conn.commit()
            return after_rule

    def audit(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[GuardrailChange]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            if rule_id is None:
                cur.execute(
                    """
                    SELECT change_id, rule_id, actor, before, after, changed_at
                    FROM tenant_guardrail_changes
                    WHERE tenant_id = %s
                    ORDER BY changed_at DESC
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT change_id, rule_id, actor, before, after, changed_at
                    FROM tenant_guardrail_changes
                    WHERE tenant_id = %s AND rule_id = %s
                    ORDER BY changed_at DESC
                    LIMIT %s
                    """,
                    (tenant_id, rule_id, limit),
                )
            return [
                GuardrailChange(
                    change_id=str(row[0]),
                    rule_id=str(row[1]),
                    actor=row[2],
                    before=row[3] if (row[3] is None or isinstance(row[3], dict)) else dict(row[3]),
                    after=row[4] if (row[4] is None or isinstance(row[4], dict)) else dict(row[4]),
                    changed_at=row[5].isoformat() if row[5] is not None else "",
                )
                for row in cur.fetchall()
            ]
