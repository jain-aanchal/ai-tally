# SPDX-License-Identifier: Apache-2.0
"""Per-org ingest API keys: mint, list (metadata only), rotate, revoke (Initiative 1, §5).

WHY this exists. Ingest keys already authenticated the ``/v1/batches`` hot path and the edge proxy
(``gateway.auth``), but there was no way for an org to mint or manage its own keys: the seed script
made the one ``local-dev`` key and that was it. Initiative 1 gives every org self-serve key
management from the dashboard, still entirely inside ai-tally's control plane (Clerk never sees a
key, Decision 2).

THE RULE OF THIS MODULE: the raw token is shown EXACTLY ONCE, at mint or rotate, and never stored.
Only its SHA-256 (``key_hash``) is persisted, hashed with the SAME ``auth.py::hash_key`` ingest uses
so a minted key authenticates byte-for-byte. ``token_prefix`` is a non-secret leading slice kept so a
human can tell two keys apart in a list; it cannot authenticate. :meth:`TenantApiKeyStore.list` never
returns ``key_hash`` or any raw token, only metadata.

Revoke is a real revoke (``revoked_at = now()``), not a DELETE, so the audit trail and any
``ON DELETE CASCADE`` history survive and a double-revoke is not an error. Rotation is one
transaction: mint the replacement and revoke the old row together, so there is never a window with
zero valid keys or two independently-committed halves.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

import psycopg

from gateway.auth import hash_key
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

#: Re-exported so a caller catching a keys-store failure catches one name (matches tenant_budgets).
TenantNotFound = TenantNotFoundError

#: Live ingest-token prefix. The scope of a key is stored separately; this prefix only marks the
#: token as an ai-tally live ingest secret so a leaked string is recognizable in a scanner.
TOKEN_PREFIX = "tally_sk_live_"

#: Characters of the random suffix surfaced in the non-secret ``token_prefix`` display slice. Six is
#: enough to disambiguate keys in a list and far too few to brute-force the rest of the token.
_DISPLAY_SUFFIX_CHARS = 6

#: Scopes a minted key may carry, matching the CHECK on ``api_keys.scope``.
KEY_SCOPES: tuple[str, ...] = ("read", "write", "admin")

#: A key name is a short human label shown in the UI, not a description.
MAX_KEY_NAME_CHARS = 120

#: A Clerk user id (``user_...``) recorded for audit. Bounded so a bad caller cannot store a blob.
MAX_CREATED_BY_CHARS = 255


class ApiKeyError(ValueError):
    """Caller-facing validation error. Surfaces as HTTP 422."""


class ApiKeyNotFoundError(ValueError):
    """The named key does not belong to this tenant (or does not exist). Surfaces as HTTP 404."""


@dataclass(frozen=True, slots=True)
class ApiKeyMeta:
    """Display metadata for one key. Deliberately carries no secret material."""

    id: str
    name: str | None
    token_prefix: str | None
    scope: str
    created_by: str | None
    created_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            # null means the key predates named keys (the seeded local-dev key), not "no name".
            "name": self.name,
            "token_prefix": self.token_prefix,
            "scope": self.scope,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # null until a best-effort off-hot-path job stamps it. Honest under uncertainty: never a
            # fabricated timestamp, never a zero. See §5.
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            # null means live; a timestamp means revoked. A real statement, not missing data.
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


@dataclass(frozen=True, slots=True)
class MintedKey:
    """A freshly minted key: the metadata plus the raw token, returned exactly once."""

    meta: ApiKeyMeta
    token: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.meta.id,
            # The raw token. Never persisted, never returned again after this response.
            "token": self.token,
            "token_prefix": self.meta.token_prefix,
            "name": self.meta.name,
            "scope": self.meta.scope,
        }


def normalize_name(value: object) -> str | None:
    """Validate an optional key name. ``None``/empty is allowed: a key need not be named."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiKeyError("name must be a string")
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_KEY_NAME_CHARS:
        raise ApiKeyError(f"name must be at most {MAX_KEY_NAME_CHARS} characters")
    return trimmed


def normalize_scope(value: object) -> str:
    """Validate a scope against :data:`KEY_SCOPES`, defaulting to ``write`` when absent."""
    if value is None:
        return "write"
    if not isinstance(value, str):
        raise ApiKeyError("scope must be a string")
    trimmed = value.strip().lower()
    if not trimmed:
        return "write"
    if trimmed not in KEY_SCOPES:
        raise ApiKeyError("scope must be one of: " + ", ".join(KEY_SCOPES))
    return trimmed


def normalize_created_by(value: object) -> str | None:
    """Validate the optional Clerk user id recorded for audit."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiKeyError("created_by must be a string")
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_CREATED_BY_CHARS:
        raise ApiKeyError(f"created_by must be at most {MAX_CREATED_BY_CHARS} characters")
    return trimmed


def _mint_token() -> tuple[str, str, str]:
    """Return ``(token, token_prefix, key_hash)`` for a fresh live ingest key.

    The token is ``tally_sk_live_`` plus a high-entropy CSPRNG suffix. ``token_prefix`` is the live
    prefix plus the first few suffix characters, a non-secret display slice. Only ``key_hash`` is
    ever stored.
    """
    suffix = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{suffix}"
    token_prefix = f"{TOKEN_PREFIX}{suffix[:_DISPLAY_SUFFIX_CHARS]}"
    return token, token_prefix, hash_key(token)


_META_COLUMNS = (
    "id, name, token_prefix, scope, created_by, created_at, last_used_at, revoked_at"
)


def _row_to_meta(row: tuple) -> ApiKeyMeta:
    return ApiKeyMeta(
        id=str(row[0]),
        name=row[1],
        token_prefix=row[2],
        scope=str(row[3]),
        created_by=row[4],
        created_at=row[5],
        last_used_at=row[6],
        revoked_at=row[7],
    )


class TenantApiKeyStore:
    """Postgres-backed key management over ``api_keys``.

    Every method resolves the caller's tenant onto ``tenants.id`` before touching a row, so the SQL
    never crosses tenants and a key id from one tenant cannot address another's key.
    """

    def __init__(self, settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[ApiKeyMeta]:
        """Every key this tenant holds, metadata only. Never returns a secret."""
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                f"SELECT {_META_COLUMNS} FROM api_keys WHERE tenant_id = %s "
                "ORDER BY created_at DESC, id",
                (resolved,),
            )
            return [_row_to_meta(row) for row in cur.fetchall()]

    def create(
        self,
        tenant_id: str,
        *,
        name: object = None,
        scope: object = "write",
        created_by: object = None,
    ) -> MintedKey:
        """Mint a new key. Returns the raw token exactly once; only its hash is stored."""
        name = normalize_name(name)
        scope = normalize_scope(scope)
        created_by = normalize_created_by(created_by)
        token, token_prefix, key_hash = _mint_token()
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                INSERT INTO api_keys (tenant_id, key_hash, scope, name, token_prefix, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, name, token_prefix, scope, created_by, created_at,
                          last_used_at, revoked_at
                """,
                (resolved, key_hash, scope, name, token_prefix, created_by),
            )
            row = cur.fetchone()
            conn.commit()
            assert row is not None  # RETURNING on an INSERT that cannot no-op
            return MintedKey(meta=_row_to_meta(row), token=token)

    def rotate(self, tenant_id: str, key_id: str, *, created_by: object = None) -> MintedKey:
        """Mint a replacement and revoke the old row in ONE transaction. Returns the new token once.

        The replacement inherits the old key's name and scope so a rotation is transparent to
        whatever the old key was for. The old token stops authenticating immediately (ingest filters
        ``revoked_at IS NULL``).
        """
        created_by = normalize_created_by(created_by)
        token, token_prefix, key_hash = _mint_token()
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "SELECT name, scope FROM api_keys "
                "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL",
                (key_id, resolved),
            )
            old = cur.fetchone()
            if old is None:
                # Either the key is not this tenant's, does not exist, or is already revoked. A
                # revoked key has nothing to rotate.
                raise ApiKeyNotFoundError(f"no live key '{key_id}' for this tenant")
            old_name, old_scope = old[0], str(old[1])
            # Carry the minter forward when the caller does not supply one, so created_by is never
            # silently blanked on rotate.
            cur.execute(
                """
                INSERT INTO api_keys (tenant_id, key_hash, scope, name, token_prefix, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, name, token_prefix, scope, created_by, created_at,
                          last_used_at, revoked_at
                """,
                (resolved, key_hash, old_scope, old_name, token_prefix, created_by),
            )
            new_row = cur.fetchone()
            cur.execute(
                "UPDATE api_keys SET revoked_at = now() "
                "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL",
                (key_id, resolved),
            )
            conn.commit()
            assert new_row is not None
            return MintedKey(meta=_row_to_meta(new_row), token=token)

    def revoke(self, tenant_id: str, key_id: str) -> bool:
        """Revoke a key (``revoked_at = now()``). Returns True if this call revoked a live key.

        A real revoke, not a delete. Double-revoke is not an error: revoking an already-revoked or
        absent key returns False rather than 404, so a double-click cannot fail.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "UPDATE api_keys SET revoked_at = now() "
                "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL",
                (key_id, resolved),
            )
            revoked = cur.rowcount > 0
            conn.commit()
            return revoked
