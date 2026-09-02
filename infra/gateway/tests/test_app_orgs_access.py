# SPDX-License-Identifier: Apache-2.0
"""Initiative 1 control-plane endpoints: provision, by-clerk-org, keys CRUD, service-token gate.

These use ``TestClient`` with in-memory fakes injected into ``app.state`` so no Postgres is needed,
matching the pattern in ``test_app_tenant_budgets.py``. They assert the four contracts the task calls
out: provision idempotency, keys CRUD (with a raw token returned exactly once and never in a list),
the resolver-backed by-clerk-org lookup, and the ``GATEWAY_SERVICE_TOKEN`` gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_api_keys import ApiKeyMeta, MintedKey
from gateway.tenant_provisioning import ProvisionResult

TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
SERVICE_TOKEN = "svc-secret-token"


class FakeProvisioner:
    def __init__(self) -> None:
        self.mapping: dict[str, tuple[str, str]] = {}
        self.calls: list[tuple[object, object]] = []

    def provision(self, *, clerk_org_id, name, region="auto") -> ProvisionResult:
        self.calls.append((clerk_org_id, name))
        if clerk_org_id in self.mapping:
            tid, plan = self.mapping[clerk_org_id]
            return ProvisionResult(tid, plan, created=False)
        tid = str(uuid.uuid4())
        self.mapping[clerk_org_id] = (tid, "free")
        return ProvisionResult(tid, "free", created=True)

    def tenant_for_clerk_org(self, clerk_org_id) -> ProvisionResult | None:
        if clerk_org_id in self.mapping:
            tid, plan = self.mapping[clerk_org_id]
            return ProvisionResult(tid, plan, created=False)
        return None


class FakeKeyStore:
    def __init__(self) -> None:
        self._rows: dict[str, ApiKeyMeta] = {}

    def list(self, tenant_id: str) -> list[ApiKeyMeta]:
        return list(self._rows.values())

    def create(self, tenant_id, *, name=None, scope="write", created_by=None) -> MintedKey:
        kid = str(uuid.uuid4())
        meta = ApiKeyMeta(
            id=kid,
            name=name,
            token_prefix="tally_sk_live_abc123",
            scope=scope,
            created_by=created_by,
            created_at=None,
            last_used_at=None,
            revoked_at=None,
        )
        self._rows[kid] = meta
        return MintedKey(meta=meta, token="tally_sk_live_abc123SECRETSUFFIX")

    def rotate(self, tenant_id, key_id, *, created_by=None) -> MintedKey:
        from gateway.tenant_api_keys import ApiKeyNotFoundError

        if key_id not in self._rows:
            raise ApiKeyNotFoundError("no live key")
        return self.create(tenant_id, name="rotated", created_by=created_by)

    def revoke(self, tenant_id, key_id) -> bool:
        return self._rows.pop(key_id, None) is not None


@contextmanager
def _client(*, auth_on: bool, token: str = SERVICE_TOKEN) -> Iterator[TestClient]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = auth_on
        app.state.settings.gateway_service_token = token if auth_on else ""
        app.state.tenant_provisioner = FakeProvisioner()
        app.state.tenant_api_keys = FakeKeyStore()
        yield client


def _svc_headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    if extra:
        h.update(extra)
    return h


# --- service-token gate ---------------------------------------------------------------------------


def test_gate_rejects_missing_token_when_auth_on() -> None:
    with _client(auth_on=True) as c:
        r = c.get("/v1/tenant/keys", headers={"X-Tenant-Id": TENANT_UUID})
        assert r.status_code == 401


def test_gate_rejects_wrong_token_when_auth_on() -> None:
    with _client(auth_on=True) as c:
        r = c.get(
            "/v1/tenant/keys",
            headers={"Authorization": "Bearer nope", "X-Tenant-Id": TENANT_UUID},
        )
        assert r.status_code == 401


def test_gate_is_noop_when_auth_off() -> None:
    # Dev escape hatch: with auth off the control plane needs no service token (matches today).
    with _client(auth_on=False) as c:
        r = c.get("/v1/tenant/keys", headers={"X-Tenant-Id": TENANT_UUID})
        assert r.status_code == 200


def test_keys_require_tenant_header() -> None:
    with _client(auth_on=True) as c:
        r = c.get("/v1/tenant/keys", headers=_svc_headers())
        assert r.status_code == 422


# --- provision ------------------------------------------------------------------------------------


def test_provision_is_idempotent() -> None:
    with _client(auth_on=True) as c:
        body = {"data": {"id": "org_abc", "name": "Acme"}}
        r1 = c.post("/v1/tenant/provision", json=body, headers=_svc_headers())
        assert r1.status_code == 200, r1.text
        first = r1.json()
        assert first["created"] is True
        assert first["plan"] == "free"

        r2 = c.post("/v1/tenant/provision", json=body, headers=_svc_headers())
        assert r2.status_code == 200
        second = r2.json()
        assert second["created"] is False
        assert second["tenant_id"] == first["tenant_id"]


def test_provision_rejects_without_service_token() -> None:
    with _client(auth_on=True) as c:
        r = c.post("/v1/tenant/provision", json={"data": {"id": "org_x", "name": "X"}})
        assert r.status_code == 401


# --- by-clerk-org ---------------------------------------------------------------------------------


def test_by_clerk_org_404_when_unmapped() -> None:
    with _client(auth_on=True) as c:
        r = c.get("/v1/tenant/by-clerk-org/org_missing", headers=_svc_headers())
        assert r.status_code == 404


def test_by_clerk_org_returns_tenant_after_provision() -> None:
    with _client(auth_on=True) as c:
        c.post(
            "/v1/tenant/provision",
            json={"data": {"id": "org_live", "name": "Live"}},
            headers=_svc_headers(),
        )
        r = c.get("/v1/tenant/by-clerk-org/org_live", headers=_svc_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["plan"] == "free"
        assert uuid.UUID(body["tenant_id"])  # a real UUID, not a name


# --- keys CRUD ------------------------------------------------------------------------------------


def test_create_key_returns_token_once_and_list_hides_it() -> None:
    with _client(auth_on=True) as c:
        r = c.post(
            "/v1/tenant/keys",
            json={"name": "prod ingest", "scope": "write"},
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID, "X-Clerk-User-Id": "user_1"}),
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["token"].startswith("tally_sk_live_")
        assert created["token_prefix"].startswith("tally_sk_live_")

        r2 = c.get("/v1/tenant/keys", headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}))
        assert r2.status_code == 200
        listed = r2.json()["keys"]
        assert len(listed) == 1
        # The list must never carry a secret.
        assert "token" not in listed[0]
        assert "key_hash" not in listed[0]
        assert listed[0]["token_prefix"].startswith("tally_sk_live_")


def test_rotate_key_returns_new_token() -> None:
    with _client(auth_on=True) as c:
        created = c.post(
            "/v1/tenant/keys",
            json={"name": "k"},
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}),
        ).json()
        r = c.post(
            f"/v1/tenant/keys/{created['id']}/rotate",
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}),
        )
        assert r.status_code == 201
        assert r.json()["token"].startswith("tally_sk_live_")


def test_rotate_unknown_key_is_404() -> None:
    with _client(auth_on=True) as c:
        r = c.post(
            f"/v1/tenant/keys/{uuid.uuid4()}/rotate",
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}),
        )
        assert r.status_code == 404


def test_revoke_key_returns_204() -> None:
    with _client(auth_on=True) as c:
        created = c.post(
            "/v1/tenant/keys",
            json={"name": "k"},
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}),
        ).json()
        r = c.delete(
            f"/v1/tenant/keys/{created['id']}",
            headers=_svc_headers({"X-Tenant-Id": TENANT_UUID}),
        )
        assert r.status_code == 204
