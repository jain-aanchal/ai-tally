# SPDX-License-Identifier: Apache-2.0
"""Resolve a caller's tenant identifier onto ``tenants.id``.

Every control-plane table keys on ``tenants(id)``, a UUID, while the dashboard and the local dev
setup identify a tenant by NAME (``local-dev``). Handing a name straight to a UUID column trips
``InvalidTextRepresentation`` deep inside psycopg, which surfaces as an opaque 500 with nothing in
it to tell an operator that the identifier was the problem.

This lived as a private copy inside ``connectors.config_admin``, which is why the endpoints written
after it (``/v1/tenant/revenue-sources/config``) 500 on a name-based caller: the rule was not
somewhere they could find it. It lives here now so every control-plane store shares one answer.
"""

from __future__ import annotations

import uuid
from typing import Any


class TenantNotFoundError(ValueError):
    """The caller's tenant identifier does not resolve to a row in ``tenants``."""


def resolve_tenant_uuid(cur: Any, tenant_id: str) -> str:
    """Pass a UUID through untouched; look a name up. Raises :class:`TenantNotFoundError`."""
    try:
        return str(uuid.UUID(tenant_id))
    except (ValueError, AttributeError, TypeError):
        pass
    cur.execute("SELECT id FROM tenants WHERE name = %s", (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise TenantNotFoundError(f"no tenant named '{tenant_id}'")
    return str(row[0])
