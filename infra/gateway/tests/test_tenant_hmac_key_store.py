# SPDX-License-Identifier: Apache-2.0
"""Restart-durability of the HMAC bootstrap store (Initiative 2 §3.2 review).

``GET /v1/tenant/hmac-key`` used to 404 for a durably-provisioned tenant after a gateway restart,
because the in-memory-only key provider lost the bytes while ``tenants.hash_salt_kek_ref`` survived.
The provider now derives material deterministically from that durable reference, so a fresh provider
(a simulated restart) resolves the same tenant's material rather than raising. This drives the real
:class:`TenantHmacKeyStore` through a fake psycopg cursor so no Postgres is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway import tenant_hmac_key
from gateway.tenant_hmac_key import TenantHmacKeyStore
from gateway.tenant_provisioning import LocalDevKeyProvider

TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"


class FakeCursor:
    """Resolves the tenant UUID (pass-through) and returns its stored hash_salt_kek_ref."""

    def __init__(self, ref: str | None) -> None:
        self._ref = ref
        self._result: tuple | None = None

    def execute(self, sql: str, params: tuple) -> None:
        if "hash_salt_kek_ref" in sql:
            self._result = (self._ref,) if self._ref is not None else None
        else:  # pragma: no cover - resolve_tenant_uuid takes the UUID fast-path, no lookup
            self._result = None

    def fetchone(self) -> tuple | None:
        return self._result

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, ref: str | None) -> None:
        self._ref = ref

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._ref)

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _store(monkeypatch, ref: str | None, provider: LocalDevKeyProvider) -> TenantHmacKeyStore:
    monkeypatch.setattr(tenant_hmac_key.psycopg, "connect", lambda _dsn: FakeConn(ref))
    return TenantHmacKeyStore(SimpleNamespace(postgres_dsn="postgresql://ignored"), provider)


def test_hmac_key_resolves_after_a_simulated_restart(monkeypatch) -> None:
    root = b"dev-root"
    # Provision under one provider instance; capture the durable reference the tenant row would hold.
    before = LocalDevKeyProvider(root_secret=root)
    ref = before.mint()
    material_before = _store(monkeypatch, ref, before).active_key(TENANT_UUID)

    # Simulated restart: a brand-new provider instance (empty minted set), same durable reference in
    # the tenant row. The store must return the SAME material, not a 404 (HmacKeyUnavailableError).
    after = LocalDevKeyProvider(root_secret=root)
    material_after = _store(monkeypatch, ref, after).active_key(TENANT_UUID)

    assert material_after.tenant_id == TENANT_UUID
    assert material_after.key_material_b64 == material_before.key_material_b64
    assert material_after.key_version == "v1"
