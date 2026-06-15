"""Seed a local tenant + API key + a couple of feature tags into Postgres.

Run after `make up`:  `python -m gateway.seed`  (or `make seed`).

Prints a freshly generated API key ONCE — only its SHA-256 is stored (api_keys.key_hash), matching
the production invariant that raw key material is never persisted. Use the printed key as the
gateway bearer token when TALLY_REQUIRE_API_KEY=true.
"""

from __future__ import annotations

import secrets

import psycopg

from gateway.auth import hash_key
from gateway.config import get_settings

_TENANT_NAME = "local-dev"
_REGION = "local"
_FEATURE_TAGS = ["assistant", "search", "summarize"]
# Cost-layer connectors enabled out of the box. Only `llm` is wired today — the rest stay un-enabled
# so the "Partial data" banner doesn't fire on a fresh stack (CTO-107).
_ENABLED_LAYERS = ["llm"]


def seed() -> None:
    settings = get_settings()
    token = "tally_sk_" + secrets.token_urlsafe(24)
    key_hash = hash_key(token)

    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        # plan tier (idempotent)
        cur.execute(
            """
            INSERT INTO plan_tiers (name, max_traces_per_month, max_features, price_micro_usd)
            VALUES ('free', 1000000, 10, 0)
            ON CONFLICT (name) DO NOTHING
            """
        )
        # tenant — reuse the existing local-dev tenant if present
        cur.execute("SELECT id FROM tenants WHERE name = %s", (_TENANT_NAME,))
        row = cur.fetchone()
        if row:
            tenant_id = row[0]
        else:
            cur.execute(
                """
                INSERT INTO tenants (name, region, plan, hash_salt_kek_ref)
                VALUES (%s, %s, 'free', %s)
                RETURNING id
                """,
                (_TENANT_NAME, _REGION, "kms://local/hmac-key-set/v1"),
            )
            tenant_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO usage_limits (tenant_id, plan) VALUES (%s, 'free')
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (tenant_id,),
        )
        cur.execute(
            "INSERT INTO api_keys (tenant_id, key_hash, scope) VALUES (%s, %s, 'write')",
            (tenant_id, key_hash),
        )
        for tag in _FEATURE_TAGS:
            cur.execute(
                """
                INSERT INTO feature_tags (tenant_id, tag, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, tag) DO NOTHING
                """,
                (tenant_id, tag, f"{tag} feature (seeded)"),
            )
        for layer in _ENABLED_LAYERS:
            cur.execute(
                """
                INSERT INTO tenant_connectors (tenant_id, layer, notes)
                VALUES (%s, %s, 'seeded')
                ON CONFLICT (tenant_id, layer) DO UPDATE SET disabled_at = NULL
                """,
                (tenant_id, layer),
            )
        conn.commit()

    print("Seeded local tenant.")
    print(f"  tenant_id : {tenant_id}")
    print(f"  api_key   : {token}")
    print("  (only its SHA-256 is stored; copy this key now — it is not recoverable.)")


if __name__ == "__main__":
    seed()
