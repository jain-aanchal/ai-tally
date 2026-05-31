"""API-key auth against the Postgres control plane.

Keys are never stored raw — ``api_keys.key_hash`` holds the SHA-256 hex of the token. We hash the
presented bearer token and look up a non-revoked row, returning its tenant_id **and scope**
(``read`` / ``write`` / ``admin``). Ingest (writing spans) requires ``write`` or ``admin``; a
read-only key is rejected with ``FORBIDDEN_SCOPE`` (CTO-33).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg

from gateway.config import Settings

# Scopes that may write ingest data. ``read`` keys can authenticate but not POST batches.
WRITE_SCOPES = frozenset({"write", "admin"})


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthResult:
    tenant_id: str
    scope: str

    @property
    def can_write(self) -> bool:
        return self.scope in WRITE_SCOPES


class ApiKeyAuth:
    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def authenticate(self, token: str) -> AuthResult | None:
        """Return the :class:`AuthResult` for a valid, non-revoked key, else None."""
        key_hash = hash_key(token)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, scope FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
                (key_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return AuthResult(tenant_id=str(row[0]), scope=str(row[1]))

    def tenant_for_key(self, token: str) -> str | None:
        """Back-compat shim: tenant_id only (kept for callers that don't need scope)."""
        result = self.authenticate(token)
        return result.tenant_id if result else None

    def ping(self) -> bool:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
