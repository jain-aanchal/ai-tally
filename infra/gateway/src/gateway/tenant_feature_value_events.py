"""Per-tenant feature value-event config (CTO-140).

Companion to :mod:`gateway.tenant_guardrails`. Each row in ``tenant_feature_value_events`` pins one
business value event (e.g. ``subscription_created``) to one ``(tenant, feature_tag)`` — the config
the /features attribution reads its ROI against. Every upsert or delete appends a row to
``tenant_feature_value_event_changes``, keyed by a client-supplied ``change_id`` UUID so a retried
request is idempotent — both the config write and the audit row are no-ops on replay.

Reads and writes both go through ``GET/POST/DELETE /v1/tenant/feature-value-events`` — the web app
never touches Postgres directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Json

from gateway.config import Settings
from gateway.tenant_lookup import resolve_tenant_uuid


@dataclass(frozen=True, slots=True)
class FeatureValueEvent:
    """One (tenant, feature_tag) -> event_name mapping."""

    feature_tag: str
    event_name: str
    created_at: str
    created_by: str | None
    notes: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_tag": self.feature_tag,
            "event_name": self.event_name,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class FeatureValueEventChange:
    """One audit row — before/after JSON snapshots of the mapping around a change."""

    change_id: str
    feature_tag: str
    actor: str | None
    before: dict[str, object] | None
    after: dict[str, object] | None
    changed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "feature_tag": self.feature_tag,
            "actor": self.actor,
            "before": self.before,
            "after": self.after,
            "changed_at": self.changed_at,
        }


def _row_to_event(row: tuple) -> FeatureValueEvent:
    return FeatureValueEvent(
        feature_tag=str(row[0]),
        event_name=str(row[1]),
        created_at=row[2].isoformat() if row[2] is not None else "",
        created_by=row[3],
        notes=row[4],
    )


class TenantFeatureValueEventStore:
    """Tiny Postgres-backed CRUD over ``tenant_feature_value_events`` + audit log.

    Every method takes the ``tenant_id`` resolved by upstream auth so the SQL never crosses tenants.
    Writes are idempotent on the client-supplied ``change_id`` — the second call with the same id is
    a no-op and returns the existing mapping unchanged.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[FeatureValueEvent]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: tenant_feature_value_events keys on the tenants.id UUID, but the dashboard
            # identifies a tenant by NAME. Fold the name onto the UUID so a name caller does not 500.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT feature_tag, event_name, created_at, created_by, notes
                FROM tenant_feature_value_events
                WHERE tenant_id = %s
                ORDER BY feature_tag
                """,
                (resolved,),
            )
            return [_row_to_event(row) for row in cur.fetchall()]

    def upsert(
        self,
        tenant_id: str,
        feature_tag: str,
        *,
        event_name: str,
        change_id: str,
        actor: str | None = None,
        notes: str | None = None,
    ) -> FeatureValueEvent:
        """Pin ``event_name`` to ``(tenant, feature_tag)``. Idempotent on ``change_id``.

        On a new change_id: capture the current row as ``before`` (NULL if absent), apply the
        upsert, then append an audit row with both before/after JSON. On a replayed change_id: no
        SQL writes — just return the existing mapping.
        """
        if not event_name:
            raise ValueError("event_name required")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: resolve a name-based tenant id onto the UUID FK once, and use it for the
            # mapping row and the audit rows alike so both key on the same tenant.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            before_event = self._fetch(cur, resolved, feature_tag)

            cur.execute(
                """
                INSERT INTO tenant_feature_value_event_changes
                    (change_id, tenant_id, feature_tag, actor, before, after)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    resolved,
                    feature_tag,
                    actor,
                    Json(before_event.as_dict()) if before_event is not None else None,
                    None,
                ),
            )
            if cur.fetchone() is None:
                conn.commit()
                current = self._fetch(cur, resolved, feature_tag)
                if current is None:
                    raise RuntimeError(
                        "change_id reserved but mapping absent — out-of-band delete?"
                    )
                return current

            cur.execute(
                """
                INSERT INTO tenant_feature_value_events
                    (tenant_id, feature_tag, event_name, created_by, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, feature_tag) DO UPDATE
                  SET event_name = EXCLUDED.event_name,
                      notes = COALESCE(EXCLUDED.notes, tenant_feature_value_events.notes)
                RETURNING feature_tag, event_name, created_at, created_by, notes
                """,
                (resolved, feature_tag, event_name, actor, notes),
            )
            row = cur.fetchone()
            assert row is not None
            after_event = _row_to_event(row)

            cur.execute(
                """
                UPDATE tenant_feature_value_event_changes
                SET after = %s
                WHERE tenant_id = %s AND change_id = %s
                """,
                (Json(after_event.as_dict()), resolved, change_id),
            )
            conn.commit()
            return after_event

    def delete(
        self,
        tenant_id: str,
        feature_tag: str,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> bool:
        """Remove the mapping for ``feature_tag``. Idempotent on ``change_id``.

        Returns True if a row was removed by this call, False if it was already absent (or the
        change_id is a replay). The audit row records the removed mapping as ``before``.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: resolve a name-based tenant id onto the UUID FK before the delete + audit.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            before_event = self._fetch(cur, resolved, feature_tag)

            cur.execute(
                """
                INSERT INTO tenant_feature_value_event_changes
                    (change_id, tenant_id, feature_tag, actor, before, after)
                VALUES (%s, %s, %s, %s, %s, NULL)
                ON CONFLICT (tenant_id, change_id) DO NOTHING
                RETURNING change_id
                """,
                (
                    change_id,
                    resolved,
                    feature_tag,
                    actor,
                    Json(before_event.as_dict()) if before_event is not None else None,
                ),
            )
            if cur.fetchone() is None:
                conn.commit()
                return False

            cur.execute(
                "DELETE FROM tenant_feature_value_events WHERE tenant_id = %s AND feature_tag = %s",
                (resolved, feature_tag),
            )
            removed = cur.rowcount > 0
            conn.commit()
            return removed

    def audit(
        self,
        tenant_id: str,
        feature_tag: str | None = None,
        limit: int = 100,
    ) -> list[FeatureValueEventChange]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # CTO-201: resolve a name-based tenant id onto the UUID FK before reading the audit log.
            resolved = resolve_tenant_uuid(cur, tenant_id)
            if feature_tag is None:
                cur.execute(
                    """
                    SELECT change_id, feature_tag, actor, before, after, changed_at
                    FROM tenant_feature_value_event_changes
                    WHERE tenant_id = %s
                    ORDER BY changed_at DESC
                    LIMIT %s
                    """,
                    (resolved, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT change_id, feature_tag, actor, before, after, changed_at
                    FROM tenant_feature_value_event_changes
                    WHERE tenant_id = %s AND feature_tag = %s
                    ORDER BY changed_at DESC
                    LIMIT %s
                    """,
                    (resolved, feature_tag, limit),
                )
            return [
                FeatureValueEventChange(
                    change_id=str(row[0]),
                    feature_tag=str(row[1]),
                    actor=row[2],
                    before=row[3] if (row[3] is None or isinstance(row[3], dict)) else dict(row[3]),
                    after=row[4] if (row[4] is None or isinstance(row[4], dict)) else dict(row[4]),
                    changed_at=row[5].isoformat() if row[5] is not None else "",
                )
                for row in cur.fetchall()
            ]

    @staticmethod
    def _fetch(cur: psycopg.Cursor, tenant_id: str, feature_tag: str) -> FeatureValueEvent | None:
        cur.execute(
            """
            SELECT feature_tag, event_name, created_at, created_by, notes
            FROM tenant_feature_value_events
            WHERE tenant_id = %s AND feature_tag = %s
            """,
            (tenant_id, feature_tag),
        )
        row = cur.fetchone()
        return _row_to_event(row) if row else None
