"""GET/POST /v1/tenant/connectors — list + toggle declared cost-layer connectors (CTO-107)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_connectors import ALLOWED_LAYERS, ConnectorDeclaration

T = "t-acme"


class FakeStore:
    """In-memory stand-in for :class:`TenantConnectorStore` — no Postgres required."""

    def __init__(self) -> None:
        # (tenant_id, layer) -> ConnectorDeclaration
        self._rows: dict[tuple[str, str], ConnectorDeclaration] = {}

    def list(self, tenant_id: str) -> list[ConnectorDeclaration]:
        return sorted(
            [r for (t, _), r in self._rows.items() if t == tenant_id],
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
