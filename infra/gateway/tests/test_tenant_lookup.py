# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared tenant-identifier resolver (CTO-201).

Every control-plane store folds a caller's tenant identifier onto ``tenants.id`` through
:func:`gateway.tenant_lookup.resolve_tenant_uuid`. This pins the cases the stores rely on: a UUID
passes through untouched, a Clerk org id and a NAME each resolve to their own tenant, and an unknown
identifier raises :class:`TenantNotFoundError` rather than reaching a UUID column and tripping the
driver. The Clerk-org and name lookups are separate ordered single-column selects (Initiative 1
review), never a combined ``name = %s OR clerk_org_id = %s`` that could straddle two rows.
"""

from __future__ import annotations

import pytest

from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"


class FakeCursor:
    """Stands in for a psycopg cursor, answering each single-column lookup from its own dict.

    Unlike a param-only fake, this reads WHICH column the resolver queried (``clerk_org_id`` vs
    ``name``) so the ordered, deterministic lookups are exercised faithfully: a value that is one
    tenant's name and another's Clerk org id resolves to the Clerk-org tenant, never ambiguously.
    """

    def __init__(
        self,
        *,
        by_clerk_org: dict[str, str] | None = None,
        by_name: dict[str, str] | None = None,
    ) -> None:
        self._by_clerk_org = by_clerk_org or {}
        self._by_name = by_name or {}
        self._last: str | None = None
        self.executed: list[str] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append(sql)
        if "clerk_org_id" in sql:
            self._last = self._by_clerk_org.get(params[0])
        elif "name" in sql:
            self._last = self._by_name.get(params[0])
        else:  # pragma: no cover - the resolver only issues the two lookups above
            self._last = None

    def fetchone(self) -> tuple | None:
        return (self._last,) if self._last is not None else None


def test_uuid_passes_through_without_a_lookup() -> None:
    cur = FakeCursor()
    assert resolve_tenant_uuid(cur, TENANT_UUID) == TENANT_UUID
    # A UUID caller must not cost a round trip to tenants.
    assert cur.executed == []


def test_name_is_resolved_to_its_uuid() -> None:
    cur = FakeCursor(by_name={TENANT_NAME: TENANT_UUID})
    assert resolve_tenant_uuid(cur, TENANT_NAME) == TENANT_UUID
    assert cur.executed, "a name must trigger the tenants lookup"


def test_clerk_org_id_is_resolved_to_its_uuid() -> None:
    cur = FakeCursor(by_clerk_org={"org_abc123": TENANT_UUID})
    assert resolve_tenant_uuid(cur, "org_abc123") == TENANT_UUID


def test_clerk_org_id_match_wins_over_a_colliding_name() -> None:
    # The dangerous case the combined OR allowed: an identifier that is tenant A's Clerk org id AND
    # tenant B's NAME. The deterministic resolver checks clerk_org_id first, so it can only ever
    # resolve to tenant A, never nondeterministically to B.
    tenant_a = "11111111-1111-4111-8111-111111111111"
    tenant_b = "22222222-2222-4222-8222-222222222222"
    cur = FakeCursor(by_clerk_org={"org_shared": tenant_a}, by_name={"org_shared": tenant_b})
    assert resolve_tenant_uuid(cur, "org_shared") == tenant_a
    # The name lookup must never run once the Clerk-org match is found (short-circuit).
    assert all("clerk_org_id" in sql for sql in cur.executed)


def test_unknown_name_raises_rather_than_reaching_a_uuid_column() -> None:
    cur = FakeCursor()
    with pytest.raises(TenantNotFoundError):
        resolve_tenant_uuid(cur, "no-such-tenant")
