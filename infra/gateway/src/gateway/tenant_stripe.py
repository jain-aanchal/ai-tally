"""Per-tenant Stripe webhook config — small Postgres surface for CTO-110.

Mirrors :mod:`gateway.tenant_connectors`: tiny, tenant-scoped CRUD, no cross-tenant queries. The
gateway's ``/v1/stripe/webhook`` endpoint reads the row to fetch the signing secret; the dashboard
calls ``/v1/tenant/stripe/connect`` to write it (paste from the Stripe Dashboard).

Storage tradeoff: the raw secret is persisted because Stripe's signing scheme requires the original
secret to recompute the expected HMAC. The 0003 migration's comment explains the production
replacement (KMS reference). See db/postgres/0003_tenant_stripe_config.sql.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class StripeConfig:
    """One ``tenant_stripe_config`` row — the secret is loaded eagerly because the webhook
    handler needs it on every delivery. Don't pass this object anywhere it might land in a log."""

    tenant_id: str
    webhook_secret: str
    stripe_account_id: str | None
    connected_at: str
    disconnected_at: str | None

    @property
    def is_active(self) -> bool:
        return self.disconnected_at is None

    def as_safe_dict(self) -> dict[str, object]:
        """Public-safe view — the secret is replaced by a fingerprint so the dashboard can
        show "connected (whsec_•••dE2k)" without ever round-tripping the raw secret."""
        suffix = self.webhook_secret[-4:] if self.webhook_secret else ""
        return {
            "tenant_id": self.tenant_id,
            "stripe_account_id": self.stripe_account_id,
            "secret_fingerprint": f"whsec_•••{suffix}" if suffix else None,
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "is_active": self.is_active,
        }


class TenantStripeStore:
    """Postgres CRUD over ``tenant_stripe_config`` + ``tenant_stripe_changes``."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> StripeConfig | None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, webhook_secret, stripe_account_id,
                       connected_at, disconnected_at
                FROM tenant_stripe_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return StripeConfig(
                tenant_id=str(row[0]),
                webhook_secret=str(row[1]),
                stripe_account_id=str(row[2]) if row[2] is not None else None,
                connected_at=row[3].isoformat() if row[3] is not None else "",
                disconnected_at=row[4].isoformat() if row[4] is not None else None,
            )

    def connect(
        self,
        tenant_id: str,
        webhook_secret: str,
        *,
        stripe_account_id: str | None = None,
        actor: str | None = None,
    ) -> StripeConfig:
        """Insert or rotate the row. Rotating is the same op as connecting again — we keep one row
        per tenant and let the audit table carry the history. Idempotent on the audit side too:
        the ``change_id`` is a deterministic UUID5 over (tenant, secret) so a tenant pasting the
        same secret twice produces only one row."""
        if not webhook_secret.startswith("whsec_"):
            raise ValueError("Stripe webhook signing secrets start with 'whsec_'")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT webhook_secret FROM tenant_stripe_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            existing = cur.fetchone()
            kind = "connected"
            if existing is not None:
                kind = "rotated" if existing[0] != webhook_secret else "connected"
            cur.execute(
                """
                INSERT INTO tenant_stripe_config
                       (tenant_id, webhook_secret, stripe_account_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                  SET webhook_secret = EXCLUDED.webhook_secret,
                      stripe_account_id = COALESCE(EXCLUDED.stripe_account_id,
                                                   tenant_stripe_config.stripe_account_id),
                      disconnected_at = NULL,
                      connected_at = CASE
                          WHEN tenant_stripe_config.disconnected_at IS NOT NULL THEN now()
                          ELSE tenant_stripe_config.connected_at
                      END
                RETURNING tenant_id, webhook_secret, stripe_account_id,
                          connected_at, disconnected_at
                """,
                (tenant_id, webhook_secret, stripe_account_id),
            )
            row = cur.fetchone()
            assert row is not None
            # Audit row — UUID5 keyed on (tenant, secret) so retried pastes don't duplicate.
            change_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"stripe-change|{tenant_id}|{kind}|{webhook_secret}"
            )
            cur.execute(
                """
                INSERT INTO tenant_stripe_changes (change_id, tenant_id, change_kind, actor)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (change_id) DO NOTHING
                """,
                (str(change_id), tenant_id, kind, actor),
            )
            conn.commit()
            return StripeConfig(
                tenant_id=str(row[0]),
                webhook_secret=str(row[1]),
                stripe_account_id=str(row[2]) if row[2] is not None else None,
                connected_at=row[3].isoformat() if row[3] is not None else "",
                disconnected_at=row[4].isoformat() if row[4] is not None else None,
            )

    def disconnect(self, tenant_id: str, *, actor: str | None = None) -> None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenant_stripe_config
                   SET disconnected_at = now()
                 WHERE tenant_id = %s AND disconnected_at IS NULL
                """,
                (tenant_id,),
            )
            change_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"stripe-change|{tenant_id}|disconnected|{conn.info.transaction_status}",
            )
            cur.execute(
                """
                INSERT INTO tenant_stripe_changes (change_id, tenant_id, change_kind, actor)
                VALUES (%s, %s, 'disconnected', %s)
                ON CONFLICT (change_id) DO NOTHING
                """,
                (str(change_id), tenant_id, actor),
            )
            conn.commit()
