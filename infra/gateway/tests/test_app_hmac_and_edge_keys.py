# SPDX-License-Identifier: Apache-2.0
"""Initiative 2 gateway endpoints: GET /v1/tenant/hmac-key and GET /v1/edge/keys.

These use ``TestClient`` with in-memory fakes injected into ``app.state`` so no Postgres is needed,
matching ``test_app_orgs_access.py``. They assert the contracts the task calls out:

* hmac-key: write/admin scope required (read is 403), tenant isolation (the key's tenant decides),
  and one tenant never receives another tenant's material.
* edge/keys: the delta advances by cursor, the payload is metadata-only (never a raw token), and the
  service-token gate protects it.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from gateway.app import app
from gateway.auth import AuthResult
from gateway.edge_keys import KeyChange
from gateway.tenant_hmac_key import HmacKeyMaterial, HmacKeyUnavailableError

SERVICE_TOKEN = "svc-secret-token"
TENANT_A = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
TENANT_B = "1d2e3f4a-5b6c-7d8e-9f01-234567890abc"


class FakeAuth:
    """Maps a token to an :class:`AuthResult`. Unknown token -> None (invalid key)."""

    def __init__(self, tokens: dict[str, AuthResult]) -> None:
        self._tokens = tokens

    def authenticate(self, token: str) -> AuthResult | None:
        return self._tokens.get(token)


class FakeHmacStore:
    """Serves per-tenant material and honors the export kill switch, without Postgres."""

    def __init__(self) -> None:
        self._material = {
            TENANT_A: b"AAAA-tenant-a-key-bytes-000000000",
            TENANT_B: b"BBBB-tenant-b-key-bytes-111111111",
        }
        self.disabled: set[str] = set()

    def export_disabled(self, tenant_id: str) -> bool:
        return tenant_id in self.disabled

    def active_key(self, tenant_id: str) -> HmacKeyMaterial:
        material = self._material.get(tenant_id)
        if material is None:
            raise HmacKeyUnavailableError(f"tenant {tenant_id} has no HMAC key reference")
        return HmacKeyMaterial(
            tenant_id=tenant_id,
            key_version="v1",
            key_material_b64=base64.b64encode(material).decode("ascii"),
        )


class FakeEdgeStore:
    """A tiny in-memory delta feed: an ordered list of (cursor, change) entries."""

    def __init__(self) -> None:
        # (watermark index, KeyChange). Cursor is the string index of the last delivered row.
        self._rows: list[KeyChange] = []

    def add(self, change: KeyChange) -> None:
        self._rows.append(change)

    def changes_since(self, cursor: str | None) -> tuple[list[KeyChange], str]:
        start = int(cursor) if cursor and cursor.isdigit() else 0
        rows = self._rows[start:]
        if not rows:
            return [], (cursor or "")
        return rows, str(len(self._rows))


@contextmanager
def _client(
    *,
    auth_on: bool,
    tokens: dict[str, AuthResult] | None = None,
    hmac: FakeHmacStore | None = None,
    edge: FakeEdgeStore | None = None,
) -> Iterator[tuple[TestClient, FakeHmacStore, FakeEdgeStore]]:
    with TestClient(app) as client:
        # Snapshot the global app.state we mutate so this module never pollutes another (the tests
        # share one process-global ``app``). Restored in the finally below.
        saved = (
            app.state.settings.require_api_key,
            app.state.settings.gateway_service_token,
            app.state.auth,
            app.state.tenant_hmac_keys,
            app.state.edge_keys,
        )
        app.state.settings.require_api_key = auth_on
        app.state.settings.gateway_service_token = SERVICE_TOKEN if auth_on else ""
        app.state.auth = FakeAuth(tokens or {})
        hmac = hmac or FakeHmacStore()
        edge = edge or FakeEdgeStore()
        app.state.tenant_hmac_keys = hmac
        app.state.edge_keys = edge
        try:
            yield client, hmac, edge
        finally:
            (
                app.state.settings.require_api_key,
                app.state.settings.gateway_service_token,
                app.state.auth,
                app.state.tenant_hmac_keys,
                app.state.edge_keys,
            ) = saved


def _write(tenant: str) -> AuthResult:
    return AuthResult(tenant_id=tenant, scope="write")


def _read(tenant: str) -> AuthResult:
    return AuthResult(tenant_id=tenant, scope="read")


# --- /v1/tenant/hmac-key ------------------------------------------------------------------------


def test_hmac_key_requires_bearer() -> None:
    with _client(auth_on=True) as (c, _h, _e):
        r = c.get("/v1/tenant/hmac-key")
        assert r.status_code == 401


def test_hmac_key_rejects_invalid_key() -> None:
    with _client(auth_on=True, tokens={}) as (c, _h, _e):
        r = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


def test_hmac_key_rejects_read_scope() -> None:
    # A read-only key authenticates but must NOT unlock hashing key material (spec §3.2).
    with _client(auth_on=True, tokens={"ro": _read(TENANT_A)}) as (c, _h, _e):
        r = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer ro"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "FORBIDDEN_SCOPE"


def test_hmac_key_returns_active_material_for_write_scope() -> None:
    with _client(auth_on=True, tokens={"wk": _write(TENANT_A)}) as (c, _h, _e):
        r = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer wk"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tenant_id"] == TENANT_A
        assert body["key_version"] == "v1"
        assert body["algorithm"] == "HMAC-SHA256"
        # Round-trips to real bytes; never a raw token.
        assert base64.b64decode(body["key_material_b64"]) == b"AAAA-tenant-a-key-bytes-000000000"


def test_hmac_key_is_tenant_isolated_never_another_tenant() -> None:
    # Two keys, two tenants: each key gets ONLY its own tenant's material, decided by the key.
    tokens = {"ka": _write(TENANT_A), "kb": _write(TENANT_B)}
    with _client(auth_on=True, tokens=tokens) as (c, _h, _e):
        ra = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer ka"}).json()
        rb = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer kb"}).json()
        assert ra["tenant_id"] == TENANT_A
        assert rb["tenant_id"] == TENANT_B
        assert ra["key_material_b64"] != rb["key_material_b64"]
        # There is no x-tenant-id input, so a header cannot coax tenant B's key out of tenant A's key.
        spoof = c.get(
            "/v1/tenant/hmac-key",
            headers={"Authorization": "Bearer ka", "X-Tenant-Id": TENANT_B},
        ).json()
        assert spoof["tenant_id"] == TENANT_A


def test_hmac_key_honors_export_kill_switch() -> None:
    store = FakeHmacStore()
    store.disabled.add(TENANT_A)
    with _client(auth_on=True, tokens={"wk": _write(TENANT_A)}, hmac=store) as (c, _h, _e):
        r = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer wk"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "HMAC_EXPORT_DISABLED"


def test_hmac_key_404_when_material_unavailable() -> None:
    with _client(auth_on=True, tokens={"wk": _write("cafef00d-0000-0000-0000-000000000000")}) as (
        c,
        _h,
        _e,
    ):
        r = c.get("/v1/tenant/hmac-key", headers={"Authorization": "Bearer wk"})
        assert r.status_code == 404


# --- /v1/edge/keys ------------------------------------------------------------------------------


def _change(key_hash: str, tenant: str, scope: str = "write", revoked: bool = False) -> KeyChange:
    at = datetime(2026, 9, 1, tzinfo=timezone.utc) if revoked else None
    return KeyChange(key_hash=key_hash, tenant_id=tenant, scope=scope, revoked_at=at)


def test_edge_keys_gate_rejects_missing_token_when_auth_on() -> None:
    with _client(auth_on=True) as (c, _h, _e):
        assert c.get("/v1/edge/keys").status_code == 401


def test_edge_keys_gate_rejects_wrong_token_when_auth_on() -> None:
    with _client(auth_on=True) as (c, _h, _e):
        r = c.get("/v1/edge/keys", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_edge_keys_gate_is_noop_when_auth_off() -> None:
    with _client(auth_on=False) as (c, _h, _e):
        assert c.get("/v1/edge/keys").status_code == 200


def test_edge_keys_delta_advances_by_cursor() -> None:
    edge = FakeEdgeStore()
    edge.add(_change("hashA", TENANT_A))
    with _client(auth_on=True, edge=edge) as (c, _h, _e):
        headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        first = c.get("/v1/edge/keys", headers=headers).json()
        assert [ch["key_hash"] for ch in first["changes"]] == ["hashA"]
        cursor = first["cursor"]

        # Nothing new: empty changes, same cursor.
        second = c.get(f"/v1/edge/keys?since={cursor}", headers=headers).json()
        assert second["changes"] == []
        assert second["cursor"] == cursor

        # A revocation arrives as a change with revoked_at set; the proxy drops it.
        edge.add(_change("hashA", TENANT_A, revoked=True))
        third = c.get(f"/v1/edge/keys?since={cursor}", headers=headers).json()
        assert len(third["changes"]) == 1
        assert third["changes"][0]["revoked_at"] is not None


def test_edge_keys_payload_is_metadata_only() -> None:
    edge = FakeEdgeStore()
    edge.add(_change("hash123", TENANT_A, scope="admin"))
    with _client(auth_on=True, edge=edge) as (c, _h, _e):
        r = c.get("/v1/edge/keys", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
        change = r.json()["changes"][0]
        # Exactly the metadata contract, and no token/token_prefix/secret leaks in.
        assert set(change.keys()) == {"key_hash", "tenant_id", "scope", "revoked_at"}
        assert change["key_hash"] == "hash123"
        assert "token" not in change
