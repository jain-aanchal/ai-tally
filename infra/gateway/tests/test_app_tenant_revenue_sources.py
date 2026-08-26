# SPDX-License-Identifier: Apache-2.0
"""/v1/tenant/revenue-sources/config — round-trip + idempotency + validation (CTO-194)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_revenue_sources import (
    RevenueSourceConfig,
    RevenueSourceConfigError,
    RevenueSourceConfigInput,
)

T = "t-acme"


class FakeRevenueSourceStore:
    """In-memory stand-in mirroring TenantRevenueSourceStore's idempotent-on-change_id contract."""

    def __init__(self) -> None:
        self._rows: dict[str, RevenueSourceConfig] = {}
        self._changes: set[tuple[str, str]] = set()

    def get(self, tenant_id: str) -> RevenueSourceConfig | None:
        return self._rows.get(tenant_id)

    def upsert(
        self,
        tenant_id: str,
        config: RevenueSourceConfigInput,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> RevenueSourceConfig:
        key = (tenant_id, change_id)
        if key in self._changes:
            # Replay: no write, return current row unchanged.
            return self._rows[tenant_id]
        self._changes.add(key)
        row = RevenueSourceConfig(
            revenue_sources=config.revenue_sources,
            include_mrr=config.include_mrr,
            created_at=None,
            updated_at=None,
            updated_by=config.updated_by,
        )
        self._rows[tenant_id] = row
        return row


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_revenue_sources = FakeRevenueSourceStore()
        yield c


def _payload(change_id: str = "chg-1", **overrides):
    base = {
        "revenue_sources": ["Stripe", "chargebee"],
        "include_mrr": True,
        "change_id": change_id,
        "updated_by": "finance@acme.test",
    }
    base.update(overrides)
    return base


def test_get_absent_returns_null_config(client: TestClient) -> None:
    # No row means "use the defaults" — the web reader must never be handed a fabricated config.
    r = client.get("/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T})
    assert r.status_code == 200, r.text
    assert r.json()["config"] is None


def test_upsert_round_trips_and_normalizes_sources(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T}, json=_payload()
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["revenue_sources"] == ["stripe", "chargebee"]

    got = client.get("/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T})
    assert got.json()["config"]["revenue_sources"] == ["stripe", "chargebee"]
    assert got.json()["config"]["include_mrr"] is True


def test_null_sources_means_every_source_counts(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/revenue-sources/config",
        headers={"X-Tenant-Id": T},
        json=_payload(revenue_sources=None),
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["revenue_sources"] is None


def test_empty_source_list_rejected(client: TestClient) -> None:
    # "Nothing is revenue" silently blanks the dashboard — the exact bug CTO-194 fixes.
    r = client.post(
        "/v1/tenant/revenue-sources/config",
        headers={"X-Tenant-Id": T},
        json=_payload(revenue_sources=[]),
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "bad",
    [
        {"revenue_sources": "stripe"},
        {"revenue_sources": [1]},
        {"revenue_sources": ["  "]},
        {"include_mrr": "yes"},
    ],
)
def test_validation_errors(client: TestClient, bad: dict) -> None:
    r = client.post(
        "/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T}, json=_payload(**bad)
    )
    assert r.status_code == 422, r.text


def test_change_id_required(client: TestClient) -> None:
    body = _payload()
    del body["change_id"]
    r = client.post("/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T}, json=body)
    assert r.status_code == 422, r.text


def test_replayed_change_id_is_a_no_op(client: TestClient) -> None:
    first = client.post(
        "/v1/tenant/revenue-sources/config", headers={"X-Tenant-Id": T}, json=_payload()
    )
    assert first.status_code == 200
    # Same change_id, different body: the replay must not apply the new values.
    replay = client.post(
        "/v1/tenant/revenue-sources/config",
        headers={"X-Tenant-Id": T},
        json=_payload(revenue_sources=["paddle"]),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["config"]["revenue_sources"] == ["stripe", "chargebee"]


def test_input_defaults_include_mrr_true() -> None:
    cfg = RevenueSourceConfigInput.from_json({"revenue_sources": None})
    assert cfg.include_mrr is True
    assert cfg.revenue_sources is None


def test_input_rejects_non_object_body() -> None:
    with pytest.raises(RevenueSourceConfigError):
        RevenueSourceConfigInput.from_json(["stripe"])
