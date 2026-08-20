"""GET/POST/DELETE /v1/tenant/feature-value-events — CRUD + idempotency (CTO-140)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_feature_value_events import (
    FeatureValueEvent,
    FeatureValueEventChange,
)

T = "t-acme"


class FakeStore:
    """In-memory stand-in for :class:`TenantFeatureValueEventStore` — no Postgres required.

    Mirrors the idempotency contract: a repeated ``change_id`` is a no-op. For upsert it returns the
    existing mapping unchanged; for delete it returns False.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], FeatureValueEvent] = {}
        self._audit: list[tuple[str, FeatureValueEventChange]] = []
        self._seen_changes: set[tuple[str, str]] = set()

    def list(self, tenant_id: str) -> list[FeatureValueEvent]:
        return sorted(
            [e for (t, _), e in self._rows.items() if t == tenant_id],
            key=lambda e: e.feature_tag,
        )

    def upsert(
        self,
        tenant_id: str,
        feature_tag: str,
        *,
        event_name: str,
        change_id: str,
        actor: str | None = None,
        notes: str | None = None,
    ) -> FeatureValueEvent:
        if not event_name:
            raise ValueError("event_name required")
        key = (tenant_id, change_id)
        before = self._rows.get((tenant_id, feature_tag))
        if key in self._seen_changes:
            assert before is not None, "change_id seen but mapping missing"
            return before
        self._seen_changes.add(key)
        now = datetime.now(tz=timezone.utc).isoformat()
        created_at = before.created_at if before else now
        event = FeatureValueEvent(
            feature_tag=feature_tag,
            event_name=event_name,
            created_at=created_at,
            created_by=actor if before is None else before.created_by,
            notes=notes if notes is not None else (before.notes if before else None),
        )
        self._rows[(tenant_id, feature_tag)] = event
        self._audit.append(
            (
                tenant_id,
                FeatureValueEventChange(
                    change_id=change_id,
                    feature_tag=feature_tag,
                    actor=actor,
                    before=before.as_dict() if before else None,
                    after=event.as_dict(),
                    changed_at=now,
                ),
            )
        )
        return event

    def delete(
        self,
        tenant_id: str,
        feature_tag: str,
        *,
        change_id: str,
        actor: str | None = None,
    ) -> bool:
        key = (tenant_id, change_id)
        if key in self._seen_changes:
            return False
        self._seen_changes.add(key)
        before = self._rows.pop((tenant_id, feature_tag), None)
        now = datetime.now(tz=timezone.utc).isoformat()
        self._audit.append(
            (
                tenant_id,
                FeatureValueEventChange(
                    change_id=change_id,
                    feature_tag=feature_tag,
                    actor=actor,
                    before=before.as_dict() if before else None,
                    after=None,
                    changed_at=now,
                ),
            )
        )
        return before is not None

    def audit(
        self,
        tenant_id: str,
        feature_tag: str | None = None,
        limit: int = 100,
    ) -> list[FeatureValueEventChange]:
        rows = [c for (t, c) in self._audit if t == tenant_id]
        if feature_tag is not None:
            rows = [c for c in rows if c.feature_tag == feature_tag]
        rows.sort(key=lambda c: c.changed_at, reverse=True)
        return rows[:limit]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_feature_value_events = FakeStore()
        yield c


def _post(client: TestClient, body: dict, tenant: str = T):
    return client.post(
        "/v1/tenant/feature-value-events",
        headers={"X-Tenant-Id": tenant},
        json=body,
    )


def _delete(client: TestClient, body: dict, tenant: str = T):
    return client.request(
        "DELETE",
        "/v1/tenant/feature-value-events",
        headers={"X-Tenant-Id": tenant},
        json=body,
    )


def test_list_empty_for_fresh_tenant(client: TestClient) -> None:
    r = client.get("/v1/tenant/feature-value-events", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == T
    assert body["value_events"] == []


def test_upsert_then_list_round_trip(client: TestClient) -> None:
    r = _post(
        client,
        {
            "feature_tag": "smart_search",
            "event_name": "subscription_created",
            "change_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["value_event"]["feature_tag"] == "smart_search"
    assert r.json()["value_event"]["event_name"] == "subscription_created"

    listing = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": T}
    ).json()
    assert len(listing["value_events"]) == 1
    assert listing["value_events"][0]["event_name"] == "subscription_created"


def test_double_post_same_change_id_is_idempotent(client: TestClient) -> None:
    body = {
        "feature_tag": "inline_writer",
        "event_name": "paid_conversion",
        "change_id": "22222222-2222-2222-2222-222222222222",
    }
    r1 = _post(client, body)
    r2 = _post(client, body)
    assert r1.status_code == 200 and r2.status_code == 200
    listing = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": T}
    ).json()
    assert len(listing["value_events"]) == 1


def test_reassign_event_for_same_feature(client: TestClient) -> None:
    _post(
        client,
        {
            "feature_tag": "smart_search",
            "event_name": "signup",
            "change_id": "33333333-3333-3333-3333-333333333333",
        },
    )
    _post(
        client,
        {
            "feature_tag": "smart_search",
            "event_name": "subscription_created",
            "change_id": "44444444-4444-4444-4444-444444444444",
        },
    )
    listing = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": T}
    ).json()
    assert len(listing["value_events"]) == 1
    assert listing["value_events"][0]["event_name"] == "subscription_created"


def test_delete_removes_mapping(client: TestClient) -> None:
    _post(
        client,
        {
            "feature_tag": "smart_search",
            "event_name": "signup",
            "change_id": "55555555-5555-5555-5555-555555555555",
        },
    )
    r = _delete(
        client,
        {
            "feature_tag": "smart_search",
            "change_id": "66666666-6666-6666-6666-666666666666",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    listing = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": T}
    ).json()
    assert listing["value_events"] == []


def test_delete_absent_is_noop_ok(client: TestClient) -> None:
    r = _delete(
        client,
        {
            "feature_tag": "never_configured",
            "change_id": "77777777-7777-7777-7777-777777777777",
        },
    )
    assert r.status_code == 200
    assert r.json()["removed"] is False


def test_missing_event_name_rejected(client: TestClient) -> None:
    r = _post(
        client,
        {
            "feature_tag": "smart_search",
            "change_id": "88888888-8888-8888-8888-888888888888",
        },
    )
    assert r.status_code == 422


def test_missing_change_id_rejected(client: TestClient) -> None:
    r = _post(
        client,
        {"feature_tag": "smart_search", "event_name": "signup"},
    )
    assert r.status_code == 422


def test_tenant_isolation(client: TestClient) -> None:
    _post(
        client,
        {
            "feature_tag": "feat_a",
            "event_name": "signup",
            "change_id": "99999999-9999-9999-9999-999999999999",
        },
        tenant="t-a",
    )
    _post(
        client,
        {
            "feature_tag": "feat_b",
            "event_name": "conversion",
            "change_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
        tenant="t-b",
    )
    a = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": "t-a"}
    ).json()
    b = client.get(
        "/v1/tenant/feature-value-events", headers={"X-Tenant-Id": "t-b"}
    ).json()
    assert [e["feature_tag"] for e in a["value_events"]] == ["feat_a"]
    assert [e["feature_tag"] for e in b["value_events"]] == ["feat_b"]


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/feature-value-events").status_code == 422
    assert (
        client.post(
            "/v1/tenant/feature-value-events",
            json={
                "feature_tag": "x",
                "event_name": "signup",
                "change_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
        ).status_code
        == 422
    )
