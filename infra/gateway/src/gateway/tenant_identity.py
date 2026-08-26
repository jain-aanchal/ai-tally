# SPDX-License-Identifier: Apache-2.0
"""Both spellings of one tenant: the ``tenants.id`` UUID and the ``tenants.name`` (CTO-185).

WHY this exists. A tenant is identified by two different strings depending on which door the
request came through. Ingest under ``require_api_key`` carries ``api_keys.tenant_id``, a UUID.
Ingest with auth off, and the dashboard on every control-plane call, carries a NAME
(``local-dev``). :func:`gateway.connectors.config_admin._resolve_tenant_uuid` already had to paper
over exactly this for the config tables, which key on the UUID.

For the config tables the mismatch is merely an awkward lookup. For anything HMAC'd it is worse,
because the tenant identifier is *key material*: ``HmacKeyRegistry`` derives a tenant's key from
the identifier string it is handed, so ``local-dev`` and ``5f1c...`` produce two unrelated key
spaces and therefore two unrelated hashes for the same account id. A lookup that guessed the wrong
spelling would return a well-formed 64-hex hash that silently matches nothing, which is the worst
possible failure: indistinguishable from an account that genuinely has no spend.

So the account-lookup endpoint does not guess. It resolves the caller's tenant to BOTH spellings
and hashes under each, and the caller matches on the set. Resolution is fail-soft: if Postgres is
unreachable we still answer with the spelling the caller gave us, because that is the one the
matching ingest path would have used anyway.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import psycopg

from gateway.config import Settings

logger = logging.getLogger("tally.gateway")

# How a tenant identifier is spelled. ``uuid`` is ``tenants.id``, ``name`` is ``tenants.name``.
FORM_UUID = "uuid"
FORM_NAME = "name"


def key_form(tenant_id: str) -> str:
    """Which spelling ``tenant_id`` is, decided purely by whether it parses as a UUID."""
    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError, TypeError):
        return FORM_NAME
    return FORM_UUID


@dataclass(frozen=True, slots=True)
class TenantKey:
    """One spelling of a tenant identifier, and therefore one HMAC key space."""

    value: str
    form: str


class TenantIdentityResolver:
    """Expands a tenant identifier into every spelling that names the same tenant row."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def key_forms(self, tenant_id: str) -> tuple[TenantKey, ...]:
        """Return the caller's spelling first, then the other one when Postgres knows it.

        Caller-first ordering is deliberate: it is the spelling the ingest path used under the
        same auth mode, so it is the likeliest match and belongs at the head of the response.

        Never raises. A missing tenant row, an unreachable control plane, or a driver error all
        degrade to the single spelling we were given rather than failing the lookup.
        """
        given = TenantKey(value=tenant_id, form=key_form(tenant_id))
        other = self._other_form(given)
        if other is None or other.value == given.value:
            return (given,)
        return (given, other)

    def _other_form(self, given: TenantKey) -> TenantKey | None:
        if not given.value:
            return None
        if given.form == FORM_UUID:
            sql, wanted = "SELECT name FROM tenants WHERE id = %s", FORM_NAME
        else:
            sql, wanted = "SELECT id FROM tenants WHERE name = %s", FORM_UUID
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(sql, (given.value,))
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - lookup is an enrichment, never a hard failure
            # Type name only. The tenant identifier is not secret, but driver messages can quote
            # the whole statement and we keep this line boring on principle.
            logger.warning("tenant identity lookup failed: %s", type(exc).__name__)
            return None
        if row is None or row[0] is None:
            return None
        return TenantKey(value=str(row[0]), form=wanted)
