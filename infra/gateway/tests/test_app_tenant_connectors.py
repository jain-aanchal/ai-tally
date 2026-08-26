"""GET/POST /v1/tenant/connectors — list + toggle declared cost-layer connectors (CTO-107)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_connectors import ALLOWED_LAYERS, ConnectorDeclaration
from gateway.tenant_lookup import TenantNotFoundError

T = "t-acme"

# CTO-201: a tenant is addressed by NAME (``local-dev``) or by its ``tenants.id`` UUID, and both
# spellings must fold onto one tenant the way the UUID foreign key forces.
TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
UNKNOWN_TENANT = "no-such-tenant"


class FakeStore:
    """In-memory stand-in for :class:`TenantConnectorStore`, no Postgres required.

    Mirrors the resolution contract the store enforces (CTO-201): the name and the UUID address one
    tenant, and an unknown name raises :class:`TenantNotFoundError` the same way the real resolver
    does. Any other id passes through unchanged so the isolation tests keep their distinct tenants.
    """

    def __init__(self) -> None:
        # (tenant_id, layer) -> ConnectorDeclaration
        self._rows: dict[tuple[str, str], ConnectorDeclaration] = {}

    def _resolve(self, tenant_id: str) -> str:
        if tenant_id == UNKNOWN_TENANT:
            raise TenantNotFoundError(f"no tenant named '{tenant_id}'")
        return TENANT_UUID if tenant_id in {TENANT_NAME, TENANT_UUID} else tenant_id

    def list(self, tenant_id: str) -> list[ConnectorDeclaration]:
        resolved = self._resolve(tenant_id)
        return sorted(
            [r for (t, _), r in self._rows.items() if t == resolved],
            key=lambda r: r.layer,
        )

    def set(
        self,
        tenant_id: str,
        layer: str,
        *,
        enabled: bool,
        notes: str | None = None,
    ) -> ConnectorDeclaration:
        if layer not in ALLOWED_LAYERS:
            raise ValueError(f"unknown layer '{layer}'")
        tenant_id = self._resolve(tenant_id)
        now = datetime.now(tz=timezone.utc).isoformat()
        existing = self._rows.get((tenant_id, layer))
        enabled_at = existing.enabled_at if existing else now
        if enabled:
            disabled_at = None
        else:
            disabled_at = now
        row = ConnectorDeclaration(
            layer=layer,
            enabled=enabled,
            enabled_at=enabled_at,
            disabled_at=disabled_at,
            notes=notes if notes is not None else (existing.notes if existing else None),
        )
        self._rows[(tenant_id, layer)] = row
        return row


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_connectors = FakeStore()
        yield c


def test_list_empty_for_fresh_tenant(client: TestClient) -> None:
    r = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == T
    assert body["connectors"] == []
    assert body["enabled_layers"] == []


def test_enable_disable_round_trip(client: TestClient) -> None:
    # Enable two layers
    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "llm", "enabled": True, "notes": "primary"},
    )
    assert r.status_code == 200
    assert r.json()["connector"]["enabled"] is True

    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "vector", "enabled": True},
    )
    assert r.status_code == 200

    listing = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": T}).json()
    assert sorted(listing["enabled_layers"]) == ["llm", "vector"]

    # Disable vector — it should remain in the list as a tombstone, but not in enabled_layers
    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "vector", "enabled": False, "notes": "turned off"},
    )
    assert r.status_code == 200
    assert r.json()["connector"]["enabled"] is False
    assert r.json()["connector"]["disabled_at"] is not None

    listing = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": T}).json()
    assert listing["enabled_layers"] == ["llm"]
    # The disabled row is still present in the audit list.
    vector_rows = [c for c in listing["connectors"] if c["layer"] == "vector"]
    assert len(vector_rows) == 1
    assert vector_rows[0]["enabled"] is False
    assert vector_rows[0]["notes"] == "turned off"


def test_re_enable_clears_disabled_at(client: TestClient) -> None:
    client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "vector", "enabled": True},
    )
    client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "vector", "enabled": False},
    )
    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "vector", "enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["connector"]["enabled"] is True
    assert r.json()["connector"]["disabled_at"] is None


def test_unknown_layer_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "quantum", "enabled": True},
    )
    assert r.status_code == 422


def test_missing_enabled_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": T},
        json={"layer": "llm"},
    )
    assert r.status_code == 422


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/connectors").status_code == 422
    assert (
        client.post(
            "/v1/tenant/connectors", json={"layer": "llm", "enabled": True}
        ).status_code
        == 422
    )


def test_tenant_isolation(client: TestClient) -> None:
    client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": "t-a"},
        json={"layer": "llm", "enabled": True},
    )
    client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": "t-b"},
        json={"layer": "vector", "enabled": True},
    )
    a = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": "t-a"}).json()
    b = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": "t-b"}).json()
    assert a["enabled_layers"] == ["llm"]
    assert b["enabled_layers"] == ["vector"]


def test_name_based_caller_does_not_500(client: TestClient) -> None:
    """A tenant NAME and its UUID address the same connector rows (CTO-201).

    ``tenant_connectors`` keys on ``tenants.id`` but the dashboard identifies a tenant as
    ``local-dev``. Without the resolver a name-based caller trips InvalidTextRepresentation deep in
    the driver and surfaces as an opaque 500, breaking the /connectors toggle in local dev.
    """
    written = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": TENANT_NAME},
        json={"layer": "llm", "enabled": True},
    )
    assert written.status_code == 200, written.text

    by_name = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": TENANT_NAME})
    by_uuid = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": TENANT_UUID})
    assert by_name.status_code == 200 and by_uuid.status_code == 200, by_uuid.text
    # A toggle set through one spelling must be visible through the other, or the two doors would
    # look like two different tenants.
    assert by_name.json()["enabled_layers"] == by_uuid.json()["enabled_layers"] == ["llm"]


def test_unknown_tenant_name_is_404_not_500(client: TestClient) -> None:
    """An unresolvable tenant name is a clean 404, never an opaque 500 from the driver (CTO-201)."""
    listed = client.get("/v1/tenant/connectors", headers={"X-Tenant-Id": UNKNOWN_TENANT})
    assert listed.status_code == 404, listed.text

    toggled = client.post(
        "/v1/tenant/connectors",
        headers={"X-Tenant-Id": UNKNOWN_TENANT},
        json={"layer": "llm", "enabled": True},
    )
    assert toggled.status_code == 404, toggled.text
