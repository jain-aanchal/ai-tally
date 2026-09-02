# SPDX-License-Identifier: Apache-2.0
"""Delta feed of api-key changes for the edge proxy's local cache (Initiative 2, §6.2).

WHY this exists. The hosted proxy resolves a real ``X-Tenant-Key`` (``tally_sk_live_...``) to a
tenant UUID on every request, and must do it without a per-request gateway round-trip to hold its
p99 < 3ms budget. So it keeps an in-memory ``sha256(key) -> {tenant_uuid, scope, revoked}`` map and
refreshes it by polling this endpoint. This module is the gateway half: it returns the key-hash to
tenant/scope changes (creations and revocations) since a caller-held cursor.

DELTA, NOT DUMP. The feed is incremental so it scales as tenants and keys grow: the steady state
ships only what changed since ``cursor``; a cold start (empty cursor) pages the full set once. The
cursor is a monotonic keyset watermark over ``(greatest(created_at, revoked_at), id)``. A revoke
stamps ``revoked_at = now()``, which raises the row's watermark above any prior cursor, so the row
reappears in the feed with its ``revoked_at`` set and the proxy drops it: revocation propagates
within one refresh interval, the proxy-path revocation SLA (spec §6.2). Keyset pagination on the
compound ``(watermark, id)`` means no row is skipped when two share a watermark, and delta
application at the proxy is idempotent, so a boundary re-send is harmless.

METADATA ONLY. Every change carries ``key_hash`` (the SHA-256 hex already stored, never reversible),
``tenant_id``, ``scope`` and ``revoked_at``. Never a raw or reversible token, never ``token_prefix``
(Initiative 1 §11). The proxy computes ``sha256`` of the presented key with the same transform
(``gateway.auth.hash_key``) and looks it up locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

#: The row's change watermark: the later of creation and revocation. A live key sits at its
#: ``created_at``; a revoked key rises to its ``revoked_at`` so the revoke re-enters the feed.
_WATERMARK_SQL = "GREATEST(created_at, COALESCE(revoked_at, created_at))"

#: Max changes returned per call. The proxy pages with the returned cursor until a short page. Large
#: enough that a cold start is a handful of round-trips, bounded so one call is never unbounded.
DEFAULT_LIMIT = 1000

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: The all-zero UUID: the low bound of the id tiebreak, so an empty cursor pages from the very start.
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True, slots=True)
class KeyChange:
    """One key's current metadata. Deliberately carries no token or reversible material."""

    key_hash: str
    tenant_id: str
    scope: str
    revoked_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "key_hash": self.key_hash,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            # null means live; a timestamp means revoked. The proxy drops any row with a timestamp.
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


def _encode_cursor(watermark: datetime, key_id: str) -> str:
    """Encode ``(watermark, id)`` as an opaque ``<epoch_micros>:<id>`` string."""
    micros = int((watermark - _EPOCH) // timedelta(microseconds=1))
    return f"{micros}:{key_id}"


def _decode_cursor(cursor: str | None) -> tuple[datetime, str]:
    """Decode a cursor into ``(watermark, id)``. An absent or malformed cursor starts from the top.

    A malformed cursor degrades to a cold start rather than raising: the proxy re-pages the full set
    (idempotent) instead of the feed 500-ing on a bad watermark.
    """
    if not cursor:
        return _EPOCH, _ZERO_UUID
    micros_str, _, key_id = cursor.partition(":")
    if not micros_str.isdigit() or not key_id:
        return _EPOCH, _ZERO_UUID
    return _EPOCH + timedelta(microseconds=int(micros_str)), key_id


class EdgeKeyStore:
    """Postgres-backed delta feed over ``api_keys`` for the edge proxy's key cache."""

    def __init__(self, settings, *, limit: int = DEFAULT_LIMIT) -> None:
        self._dsn = settings.postgres_dsn
        self._limit = limit

    def changes_since(self, cursor: str | None) -> tuple[list[KeyChange], str]:
        """Return ``(changes, next_cursor)`` for key rows changed since ``cursor``.

        ``next_cursor`` advances to the last row's watermark when anything is returned, and is the
        input cursor unchanged when nothing changed (so a steady-state poll ships an empty list and
        the same cursor).
        """
        after_wm, after_id = _decode_cursor(cursor)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT key_hash, tenant_id, scope, revoked_at, {_WATERMARK_SQL} AS wm, id
                FROM api_keys
                WHERE ({_WATERMARK_SQL}, id) > (%s, %s)
                ORDER BY wm, id
                LIMIT %s
                """,
                (after_wm, after_id, self._limit),
            )
            rows = cur.fetchall()
        if not rows:
            # Nothing changed: echo the caller's cursor (normalized empty -> "") so it can persist it.
            return [], (cursor or "")
        changes = [
            KeyChange(
                key_hash=str(row[0]),
                tenant_id=str(row[1]),
                scope=str(row[2]),
                revoked_at=row[3],
            )
            for row in rows
        ]
        last = rows[-1]
        return changes, _encode_cursor(last[4], str(last[5]))
