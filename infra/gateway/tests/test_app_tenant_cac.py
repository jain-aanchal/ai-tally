"""/v1/tenant/cac — CRUD + CSV round-trip + sanity guard + period lock (CTO-111)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_cac import CacFormInput, CacPeriod, CacPeriodError, _end_of_month

T = "t-acme"


class FakeCacStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], CacPeriod] = {}

    def list(self, tenant_id: str) -> list[CacPeriod]:
        out = [r for (t, _), r in self._rows.items() if t == tenant_id]
        return sorted(out, key=lambda r: r.period_start, reverse=True)

    def upsert(self, tenant_id: str, form: CacFormInput) -> CacPeriod:
        form.sanity_check()
        existing = self._rows.get((tenant_id, form.period_start))
        if existing and existing.closed_at is not None:
            raise CacPeriodError(
                f"period {form.period_start.isoformat()} is locked (closed_at is set)"
            )
        period = CacPeriod(
            period_start=form.period_start,
            period_end=_end_of_month(form.period_start),
            currency="USD",
            paid_spend_micro_usd=form.paid_spend_micro_usd,
            sales_spend_micro_usd=form.sales_spend_micro_usd,
            content_spend_micro_usd=form.content_spend_micro_usd,
            overhead_micro_usd=form.overhead_micro_usd,
            new_customers_paid=form.new_customers_paid,
            new_customers_total=form.new_customers_total,
            notes=form.notes,
            closed_at=None,
        )
        self._rows[(tenant_id, form.period_start)] = period
        for (t, ps), row in list(self._rows.items()):
            if t == tenant_id and ps < form.period_start and row.closed_at is None:
                self._rows[(t, ps)] = replace(row, closed_at=datetime.now(timezone.utc))
        return period

    def upsert_many(self, tenant_id, forms):
        return [self.upsert(tenant_id, f) for f in forms]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_cac = FakeCacStore()
        yield c


def _payload(period_start="2026-01-01", **overrides):
    base = {
        "period_start": period_start,
        "paid_spend_micro_usd": 5_000_000_000,
        "sales_spend_micro_usd": 8_000_000_000,
        "content_spend_micro_usd": 2_000_000_000,
        "overhead_micro_usd": 3_000_000_000,
        "new_customers_paid": 40,
        "new_customers_total": 120,
        "notes": "Q1 push",
    }
    base.update(overrides)
    return base


def test_round_trip_form(client: TestClient) -> None:
    r = client.post("/v1/tenant/cac", headers={"X-Tenant-Id": T}, json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"]["period_start"] == "2026-01-01"
    assert body["period"]["period_end"] == "2026-01-31"
    assert body["period"]["paid_spend_micro_usd"] == 5_000_000_000
    assert body["period"]["locked"] is False

    listing = client.get("/v1/tenant/cac", headers={"X-Tenant-Id": T}).json()
    assert len(listing["periods"]) == 1
    assert listing["periods"][0]["new_customers_total"] == 120


def test_sanity_guard_rejects_paid_gt_total(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/cac",
        headers={"X-Tenant-Id": T},
        json=_payload(new_customers_paid=200, new_customers_total=120),
    )
    assert r.status_code == 422
    assert "new_customers_total" in r.text


def test_period_locks_when_successor_arrives(client: TestClient) -> None:
    client.post("/v1/tenant/cac", headers={"X-Tenant-Id": T}, json=_payload("2026-01-01"))
    client.post("/v1/tenant/cac", headers={"X-Tenant-Id": T}, json=_payload("2026-02-01"))
    listing = client.get("/v1/tenant/cac", headers={"X-Tenant-Id": T}).json()
    by_start = {p["period_start"]: p for p in listing["periods"]}
    assert by_start["2026-01-01"]["locked"] is True
    assert by_start["2026-02-01"]["locked"] is False

    r = client.post(
        "/v1/tenant/cac",
        headers={"X-Tenant-Id": T},
        json=_payload("2026-01-01", paid_spend_micro_usd=9_999_999),
    )
    assert r.status_code == 422
    assert "locked" in r.text


def test_cross_tenant_isolation(client: TestClient) -> None:
    client.post("/v1/tenant/cac", headers={"X-Tenant-Id": "t-a"}, json=_payload())
    client.post(
        "/v1/tenant/cac",
        headers={"X-Tenant-Id": "t-b"},
        json=_payload("2026-02-01", paid_spend_micro_usd=1_000),
    )
    a = client.get("/v1/tenant/cac", headers={"X-Tenant-Id": "t-a"}).json()
    b = client.get("/v1/tenant/cac", headers={"X-Tenant-Id": "t-b"}).json()
    assert len(a["periods"]) == 1
    assert len(b["periods"]) == 1
    assert a["periods"][0]["period_start"] == "2026-01-01"
    assert b["periods"][0]["period_start"] == "2026-02-01"


def test_csv_round_trip(client: TestClient) -> None:
    t = client.get("/v1/tenant/cac/csv/template", headers={"X-Tenant-Id": T})
    assert t.status_code == 200
    template = t.text
    assert template.startswith("period_start,paid,sales,content,overhead,")

    up = client.post(
        "/v1/tenant/cac/csv",
        headers={"X-Tenant-Id": T, "content-type": "text/csv"},
        content=template,
    )
    assert up.status_code == 200, up.text
    assert up.json()["imported"] == 1

    listing = client.get("/v1/tenant/cac", headers={"X-Tenant-Id": T}).json()
    assert listing["periods"][0]["period_start"] == "2026-01-01"
    assert listing["periods"][0]["paid_spend_micro_usd"] == 5_000_000_000


def test_csv_rejects_bad_header(client: TestClient) -> None:
    bad = "period_start,paid,sales\n2026-01-01,1,2\n"
    r = client.post(
        "/v1/tenant/cac/csv",
        headers={"X-Tenant-Id": T, "content-type": "text/csv"},
        content=bad,
    )
    assert r.status_code == 422


def test_csv_surfaces_row_number_in_error(client: TestClient) -> None:
    body = (
        "period_start,paid,sales,content,overhead,customers_paid,customers_total,notes\n"
        "2026-01-01,1,2,3,4,1,5,ok\n"
        "2026-02-01,1,2,3,4,99,5,bad\n"
    )
    r = client.post(
        "/v1/tenant/cac/csv",
        headers={"X-Tenant-Id": T, "content-type": "text/csv"},
        content=body,
    )
    assert r.status_code == 422
    assert "row 3" in r.text


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/cac").status_code == 422
    assert client.post("/v1/tenant/cac", json=_payload()).status_code == 422
