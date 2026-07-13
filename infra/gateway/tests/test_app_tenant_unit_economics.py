"""/v1/tenant/unit-economics/config — round-trip + idempotency + validation (CTO-126)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_unit_economics import (
    UnitEconomicsConfig,
    UnitEconomicsConfigError,
    UnitEconomicsConfigInput,
)

T = "t-acme"


class FakeUnitEconomicsStore:
    """In-memory stand-in mirroring TenantUnitEconomicsStore's idempotent-on-change_id contract."""

    def __init__(self) -> None:
        self._rows: dict[str, UnitEconomicsConfig] = {}
        self._changes: set[tuple[str, str]] = set()

    def get(self, tenant_id: str) -> UnitEconomicsConfig | None:
        return self._rows.get(tenant_id)

    def upsert(
        self,
        tenant_id: str,
        config: UnitEconomicsConfigInput,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> UnitEconomicsConfig:
        config.sanity_check()
        key = (tenant_id, change_id)
        if key in self._changes:
            # Replay: no write, return current row unchanged.
            return self._rows[tenant_id]
        self._changes.add(key)
        row = UnitEconomicsConfig(
            ltv_cac_green_threshold=config.ltv_cac_green_threshold,
            ltv_cac_yellow_threshold=config.ltv_cac_yellow_threshold,
            payback_months_green=config.payback_months_green,
            payback_months_yellow=config.payback_months_yellow,
            created_at=None,
            updated_at=None,
            updated_by=config.updated_by,
        )
        self._rows[tenant_id] = row
        return row


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_unit_economics = FakeUnitEconomicsStore()
        yield c


def _payload(change_id="chg-1", **overrides):
    base = {
        "ltv_cac_green_threshold": 4.0,
        "ltv_cac_yellow_threshold": 1.5,
        "payback_months_green": 10,
        "payback_months_yellow": 20,
        "change_id": change_id,
        "updated_by": "finance@acme.test",
    }
    base.update(overrides)
    return base


def test_get_absent_returns_null_config(client: TestClient) -> None:
    r = client.get("/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": T})
    assert r.status_code == 200, r.text
    assert r.json()["config"] is None


def test_round_trip(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": T}, json=_payload()
    )
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["ltv_cac_green_threshold"] == 4.0
    assert cfg["payback_months_yellow"] == 20
    assert cfg["updated_by"] == "finance@acme.test"

    got = client.get("/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": T}).json()
    assert got["config"]["ltv_cac_yellow_threshold"] == 1.5
    assert got["config"]["payback_months_green"] == 10


def test_idempotent_on_change_id(client: TestClient) -> None:
    # First write sets 4.0 green.
    client.post("/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": T}, json=_payload())
    # Replay of the SAME change_id with a different green value is a no-op.
    replay = client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": T},
        json=_payload(ltv_cac_green_threshold=9.0),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["config"]["ltv_cac_green_threshold"] == 4.0

    # A NEW change_id does apply the change.
    applied = client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": T},
        json=_payload(change_id="chg-2", ltv_cac_green_threshold=5.0),
    )
    assert applied.json()["config"]["ltv_cac_green_threshold"] == 5.0


def test_rejects_inverted_ltv_cac_bands(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": T},
        json=_payload(ltv_cac_green_threshold=1.0, ltv_cac_yellow_threshold=3.0),
    )
    assert r.status_code == 422
    assert "ltv_cac_green_threshold" in r.text


def test_rejects_inverted_payback_bands(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": T},
        json=_payload(payback_months_green=24, payback_months_yellow=12),
    )
    assert r.status_code == 422
    assert "payback_months_green" in r.text


def test_requires_change_id(client: TestClient) -> None:
    body = _payload()
    del body["change_id"]
    r = client.post("/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": T}, json=body)
    assert r.status_code == 422
    assert "change_id" in r.text


def test_rejects_non_numeric_threshold(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": T},
        json=_payload(ltv_cac_green_threshold="abc"),
    )
    assert r.status_code == 422


def test_cross_tenant_isolation(client: TestClient) -> None:
    client.post(
        "/v1/tenant/unit-economics/config",
        headers={"X-Tenant-Id": "t-a"},
        json=_payload(change_id="a-1", ltv_cac_green_threshold=4.0),
    )
    b = client.get("/v1/tenant/unit-economics/config", headers={"X-Tenant-Id": "t-b"}).json()
    assert b["config"] is None


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/unit-economics/config").status_code == 422
    assert (
        client.post("/v1/tenant/unit-economics/config", json=_payload()).status_code == 422
    )


def test_input_from_json_direct() -> None:
    # Guard the parser directly so validation isn't only exercised through HTTP.
    with pytest.raises(UnitEconomicsConfigError):
        UnitEconomicsConfigInput.from_json({"ltv_cac_green_threshold": 3.0})
