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

SAFE-LAG WINDOW (Initiative 2 §6.2 review). ``created_at`` / ``revoked_at`` are stamped at statement
time, NOT commit time, so a slow transaction can commit a row whose watermark is BELOW a cursor that
a later-but-faster commit already advanced past. Under plain keyset pagination that row is invisible
forever (its watermark is not ``> cursor``), so the proxy never caches the key and the customer's
first proxy events never land. A monotonic sequence would not help: a sequence value is also assigned
before commit, so it can commit out of order the same way. The fix is time-based: the feed never
returns (and so never advances the cursor past) a row whose watermark is newer than ``now() - lag``.
Any transaction that commits within ``lag`` is therefore still ahead of the cursor on the next poll
and gets picked up. See ``Settings.edge_key_safe_lag_seconds`` for how the margin is sized.

METADATA ONLY. Every change carries ``key_hash`` (the SHA-256 hex already stored, never reversible),
``tenant_id``, ``scope`` and ``revoked_at``. Never a raw or reversible token, never ``token_prefix``
(Initiative 1 §11). The proxy computes ``sha256`` of the presented key with the same transform
(``gateway.auth.hash_key``) and looks it up locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

#: The row's change watermark: the latest of creation, revocation and an edge-visible settings
#: change. A live key sits at its ``created_at``; a revoked key rises to its ``revoked_at`` so the
#: revoke re-enters the feed; and turning the org's hosted proxy on or off stamps ``edge_updated_at``
#: on its live keys, so they re-enter carrying the new ``proxy_enabled``. MUST match the expression in
#: ``idx_api_keys_edge_watermark_v2`` (0033) or every poll becomes a sequential scan.
_WATERMARK_SQL = (
    "GREATEST(k.created_at, COALESCE(k.revoked_at, k.created_at), COALESCE(k.edge_updated_at, k.created_at))"
)

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
    #: Whether the key's organization has the hosted proxy turned on (0033). Defaults to False, the
    #: same default as a tenant with no ``tenant_proxy_config`` row, so a change built without it
    #: refuses the proxy rather than silently allowing it.
    proxy_enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "key_hash": self.key_hash,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            # null means live; a timestamp means revoked. The proxy drops any row with a timestamp.
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            # Always sent. The proxy treats an ABSENT field as "this gateway has no per-org switch" and
            # allows the key, so omitting it here would quietly bypass an admin's "off".
            "proxy_enabled": self.proxy_enabled,
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
        # Safe-lag margin (seconds): the feed never advances the cursor past ``now() - this``, so a
        # row committed out of watermark order (a slow transaction) is still picked up on a later
        # poll. See the module docstring and Settings.edge_key_safe_lag_seconds.
        self._safe_lag_seconds = float(getattr(settings, "edge_key_safe_lag_seconds", 5.0))

    def changes_since(self, cursor: str | None) -> tuple[list[KeyChange], str]:
        """Return ``(changes, next_cursor)`` for key rows changed since ``cursor``.

        ``next_cursor`` advances to the last row's watermark when anything is returned, and is the
        input cursor unchanged when nothing changed (so a steady-state poll ships an empty list and
        the same cursor).

        Only rows whose watermark is at least ``edge_key_safe_lag_seconds`` old (per the DATABASE
        clock, not the app's) are eligible, so a row that commits out of watermark order within the
        lag window is never stranded below an already-advanced cursor.
        """
        after_wm, after_id = _decode_cursor(cursor)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT k.key_hash, k.tenant_id, k.scope, k.revoked_at, {_WATERMARK_SQL} AS wm, k.id,
                       COALESCE(c.enabled, false) AS proxy_enabled
                FROM api_keys k
                LEFT JOIN tenant_proxy_config c ON c.tenant_id = k.tenant_id
                WHERE ({_WATERMARK_SQL}, k.id) > (%s, %s)
                  AND {_WATERMARK_SQL} <= now() - make_interval(secs => %s)
                ORDER BY wm, k.id
                LIMIT %s
                """,
                (after_wm, after_id, self._safe_lag_seconds, self._limit),
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
                proxy_enabled=bool(row[6]),
            )
            for row in rows
        ]
        last = rows[-1]
        return changes, _encode_cursor(last[4], str(last[5]))
