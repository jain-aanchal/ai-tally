"""GET/POST /v1/tenant/guardrails — control-plane CRUD + idempotent audit (CTO-116)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_guardrails import (
    ALLOWED_KINDS,
    ALLOWED_STATES,
    GuardrailChange,
    GuardrailRule,
)

T = "t-acme"


class FakeStore:
    """In-memory stand-in for :class:`TenantGuardrailStore` — no Postgres required.

    Mirrors the idempotency contract: a repeated ``change_id`` is a no-op and returns the
    existing rule unchanged.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], GuardrailRule] = {}
        self._audit: list[tuple[str, GuardrailChange]] = []  # (tenant_id, change)
        self._seen_changes: set[tuple[str, str]] = set()  # (tenant_id, change_id)

    def list(self, tenant_id: str) -> list[GuardrailRule]:
        return sorted(
            [r for (t, _), r in self._rows.items() if t == tenant_id],
            key=lambda r: r.rule_id,
        )

    def upsert(
        self,
        tenant_id: str,
        rule_id: str,
        *,
        kind: str,
        params: dict,
        state: str,
        change_id: str,
        actor: str | None = None,
        notes: str | None = None,
    ) -> GuardrailRule:
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unknown kind '{kind}'")
        if state not in ALLOWED_STATES:
            raise ValueError(f"unknown state '{state}'")
        key = (tenant_id, change_id)
        if key in self._seen_changes:
            # Replay — return the existing rule unchanged.
            existing = self._rows.get((tenant_id, rule_id))
            assert existing is not None, "change_id seen but rule missing"
            return existing
        self._seen_changes.add(key)
        before = self._rows.get((tenant_id, rule_id))
        now = datetime.now(tz=timezone.utc).isoformat()
        created_at = before.created_at if before else now
        rule = GuardrailRule(
            rule_id=rule_id,
            kind=kind,
            params=dict(params),
            state=state,
            created_at=created_at,
            updated_at=now,
            created_by=actor if before is None else before.created_by,
            notes=notes if notes is not None else (before.notes if before else None),
        )
        self._rows[(tenant_id, rule_id)] = rule
        self._audit.append(
            (
                tenant_id,
                GuardrailChange(
                    change_id=change_id,
                    rule_id=rule_id,
                    actor=actor,
                    before=before.as_dict() if before else None,
                    after=rule.as_dict(),
                    changed_at=now,
                ),
            )
        )
        return rule

    def audit(
        self,
        tenant_id: str,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[GuardrailChange]:
        rows = [c for (t, c) in self._audit if t == tenant_id]
        if rule_id is not None:
            rows = [c for c in rows if c.rule_id == rule_id]
        rows.sort(key=lambda c: c.changed_at, reverse=True)
        return rows[:limit]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_guardrails = FakeStore()
        yield c


def _post(client: TestClient, body: dict, tenant: str = T):
    return client.post(
        "/v1/tenant/guardrails",
        headers={"X-Tenant-Id": tenant},
        json=body,
    )


def test_list_empty_for_fresh_tenant(client: TestClient) -> None:
    r = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == T
    assert body["rules"] == []


def test_upsert_then_list_round_trip(client: TestClient) -> None:
    r = _post(
        client,
        {
            "rule_id": "gr_cost",
            "kind": "cost_cap",
            "state": "shadow",
            "params": {"max_cost_micro_usd": 1_000_000},
            "change_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["rule"]["rule_id"] == "gr_cost"
    assert r.json()["rule"]["state"] == "shadow"

    listing = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": T}).json()
    assert len(listing["rules"]) == 1
    assert listing["rules"][0]["kind"] == "cost_cap"
    assert listing["rules"][0]["params"] == {"max_cost_micro_usd": 1_000_000}


def test_double_post_same_change_id_is_idempotent(client: TestClient) -> None:
    body = {
        "rule_id": "gr_pii",
        "kind": "pii_gate",
        "state": "shadow",
        "params": {},
        "change_id": "22222222-2222-2222-2222-222222222222",
    }
    r1 = _post(client, body)
    r2 = _post(client, body)
    assert r1.status_code == 200 and r2.status_code == 200
    # Listing is stable.
    listing = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": T}).json()
    assert len(listing["rules"]) == 1
    # Audit has only one row for this rule.
    audit = client.get(
        "/v1/tenant/guardrails/audit",
        headers={"X-Tenant-Id": T},
        params={"rule_id": "gr_pii"},
    ).json()
    assert len(audit["changes"]) == 1


def test_state_transition_shadow_to_enabled(client: TestClient) -> None:
    r = _post(
        client,
        {
            "rule_id": "gr_loop",
            "kind": "loop_limit",
            "state": "shadow",
            "params": {"max_steps": 50},
            "change_id": "33333333-3333-3333-3333-333333333333",
        },
    )
    assert r.status_code == 200
    r = _post(
        client,
        {
            "rule_id": "gr_loop",
            "kind": "loop_limit",
            "state": "enabled",
            "params": {"max_steps": 50},
            "change_id": "44444444-4444-4444-4444-444444444444",
        },
    )
    assert r.status_code == 200
    listing = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": T}).json()
    assert len(listing["rules"]) == 1
    assert listing["rules"][0]["state"] == "enabled"


def test_audit_log_appended(client: TestClient) -> None:
    for i, state in enumerate(["shadow", "enabled", "disabled"], start=1):
        r = _post(
            client,
            {
                "rule_id": "gr_dep",
                "kind": "model_deprecation",
                "state": state,
                "params": {"deprecated_models": ["claude-2"]},
                "change_id": f"5555555{i}-5555-5555-5555-555555555555",
            },
        )
        assert r.status_code == 200
    audit = client.get(
        "/v1/tenant/guardrails/audit",
        headers={"X-Tenant-Id": T},
        params={"rule_id": "gr_dep"},
    ).json()
    assert len(audit["changes"]) == 3
    # Newest first.
    assert audit["changes"][0]["after"]["state"] == "disabled"


def test_unknown_kind_rejected(client: TestClient) -> None:
    r = _post(
        client,
        {
            "rule_id": "gr_x",
            "kind": "quantum_gate",
            "state": "shadow",
            "params": {},
            "change_id": "66666666-6666-6666-6666-666666666666",
        },
    )
    assert r.status_code == 422


def test_unknown_state_rejected(client: TestClient) -> None:
    r = _post(
        client,
        {
            "rule_id": "gr_x",
            "kind": "cost_cap",
            "state": "yelling",
            "params": {},
            "change_id": "77777777-7777-7777-7777-777777777777",
        },
    )
    assert r.status_code == 422


def test_missing_change_id_rejected(client: TestClient) -> None:
    r = _post(
        client,
        {
            "rule_id": "gr_x",
            "kind": "cost_cap",
            "state": "shadow",
            "params": {},
        },
    )
    assert r.status_code == 422


def test_tenant_isolation(client: TestClient) -> None:
    _post(
        client,
        {
            "rule_id": "gr_a",
            "kind": "cost_cap",
            "state": "shadow",
            "params": {},
            "change_id": "88888888-8888-8888-8888-888888888888",
        },
        tenant="t-a",
    )
    _post(
        client,
        {
            "rule_id": "gr_b",
            "kind": "pii_gate",
            "state": "enabled",
            "params": {},
            "change_id": "99999999-9999-9999-9999-999999999999",
        },
        tenant="t-b",
    )
    a = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": "t-a"}).json()
    b = client.get("/v1/tenant/guardrails", headers={"X-Tenant-Id": "t-b"}).json()
    assert [r["rule_id"] for r in a["rules"]] == ["gr_a"]
    assert [r["rule_id"] for r in b["rules"]] == ["gr_b"]


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/guardrails").status_code == 422
    assert (
        client.post(
            "/v1/tenant/guardrails",
            json={
                "rule_id": "x",
                "kind": "cost_cap",
                "state": "shadow",
                "params": {},
                "change_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        ).status_code
        == 422
    )
