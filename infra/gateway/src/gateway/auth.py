"""API-key auth against the Postgres control plane.

Keys are never stored raw — ``api_keys.key_hash`` holds the SHA-256 hex of the token. We hash the
presented bearer token and look up a non-revoked row, returning its tenant_id.
"""

from __future__ import annotations

import hashlib

import psycopg

from gateway.config import Settings


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiKeyAuth:
    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def tenant_for_key(self, token: str) -> str | None:
        """Return the tenant_id (UUID str) for a valid, non-revoked key, else None."""
        key_hash = hash_key(token)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
                (key_hash,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def ping(self) -> bool:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
