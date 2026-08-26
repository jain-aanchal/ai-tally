# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared tenant-identifier resolver (CTO-201).

Every control-plane store folds a caller's tenant identifier onto ``tenants.id`` through
:func:`gateway.tenant_lookup.resolve_tenant_uuid`. This pins the three cases the stores rely on: a
UUID passes through untouched, a NAME is looked up in ``tenants.name``, and an unknown identifier
raises :class:`TenantNotFoundError` rather than reaching a UUID column and tripping the driver.
"""

from __future__ import annotations

import pytest

from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"


class FakeCursor:
    """Stands in for a psycopg cursor, answering the resolver's single name lookup from a dict."""

    def __init__(self, names_to_ids: dict[str, str]) -> None:
        self._names = names_to_ids
        self._last_param: str | None = None
        self.executed: list[str] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append(sql)
        self._last_param = params[0]

    def fetchone(self) -> tuple | None:
        value = self._names.get(self._last_param)
        return (value,) if value is not None else None


def test_uuid_passes_through_without_a_lookup() -> None:
    cur = FakeCursor({})
    assert resolve_tenant_uuid(cur, TENANT_UUID) == TENANT_UUID
    # A UUID caller must not cost a round trip to tenants.
    assert cur.executed == []


def test_name_is_resolved_to_its_uuid() -> None:
    cur = FakeCursor({TENANT_NAME: TENANT_UUID})
    assert resolve_tenant_uuid(cur, TENANT_NAME) == TENANT_UUID
    assert cur.executed, "a name must trigger the tenants lookup"


def test_unknown_name_raises_rather_than_reaching_a_uuid_column() -> None:
    cur = FakeCursor({})
    with pytest.raises(TenantNotFoundError):
        resolve_tenant_uuid(cur, "no-such-tenant")
