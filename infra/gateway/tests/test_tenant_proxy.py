# SPDX-License-Identifier: Apache-2.0
"""The per-organization hosted-proxy switch (0033): the store, the control-plane routes, and the
invariant that ties the feed's watermark SQL to the index that serves it."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway import edge_keys, tenant_proxy
from gateway.app import app
from gateway.tenant_proxy import DEFAULT_CONFIG, ProxyConfig, TenantProxyStore

T = "t-acme"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


# --- store ----------------------------------------------------------------------------------------


class FakeDB:
    def __init__(self) -> None:
        self.row: tuple | None = None
        self.statements: list[tuple[str, tuple]] = []
        self.commits = 0
        self.connects = 0


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self._db = db
        self._one: tuple | None = None

    def execute(self, sql: str, params: tuple) -> None:
        flat = " ".join(sql.split())
        self._db.statements.append((flat, params))
        if flat.startswith("SELECT enabled"):
            self._one = self._db.row
        elif flat.startswith("INSERT INTO tenant_proxy_config"):
            self._db.row = (params[1], NOW)
            self._one = self._db.row
        else:
            self._one = None

    def fetchone(self) -> tuple | None:
        return self._one

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._db)

    def commit(self) -> None:
        self._db.commits += 1

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def db(monkeypatch) -> FakeDB:
    fake = FakeDB()

    def _connect(_dsn: str) -> FakeConn:
        fake.connects += 1
        return FakeConn(fake)

    monkeypatch.setattr(tenant_proxy.psycopg, "connect", _connect)
    monkeypatch.setattr(tenant_proxy, "resolve_tenant_uuid", lambda _cur, tenant: tenant)
    return fake


def _store() -> TenantProxyStore:
    return TenantProxyStore(SimpleNamespace(postgres_dsn="postgresql://ignored"))


def test_a_tenant_that_never_set_it_is_off(db: FakeDB) -> None:
    assert _store().get(T) == DEFAULT_CONFIG
    assert DEFAULT_CONFIG.enabled is False


def test_toggle_writes_config_and_re_emits_keys_in_one_transaction(db: FakeDB) -> None:
    cfg = _store().set_enabled(T, True, updated_by="user_123")
    assert cfg == ProxyConfig(enabled=True, updated_at=NOW)
    kinds = [stmt.split(" ")[0] for stmt, _ in db.statements]
    assert kinds == ["INSERT", "UPDATE"]
    update_sql, update_params = db.statements[1]
    # The stamp is what carries the change into a running proxy's key cache; only live keys need it.
    assert "SET edge_updated_at = now()" in update_sql
    assert "revoked_at IS NULL" in update_sql
    assert update_params == (T,)
    # ONE commit covering both. Split commits let a proxy poll land between them and cache the old
    # value against a watermark that has already moved past it.
    assert db.commits == 1
    assert db.connects == 1


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, "off"])
def test_non_boolean_is_refused_before_touching_the_database(db: FakeDB, value: object) -> None:
    # "false" is truthy in Python. Coercing it would switch a tenant's proxy ON when the caller meant off.
    with pytest.raises(ValueError):
        _store().set_enabled(T, value)
    assert db.connects == 0


def test_oversized_audit_id_does_not_block_the_toggle(db: FakeDB) -> None:
    _store().set_enabled(T, False, updated_by="u" * 500)
    _insert_sql, insert_params = db.statements[0]
    assert insert_params == (T, False, None)


# --- routes ---------------------------------------------------------------------------------------


class FakeProxyStore:
    """In-memory TenantProxyStore with the same boolean contract."""

    def __init__(self) -> None:
        self.cfg: dict[str, ProxyConfig] = {}
        self.updated_by: str | None = None

    def get(self, tenant_id: str) -> ProxyConfig:
        return self.cfg.get(tenant_id, DEFAULT_CONFIG)

    def set_enabled(self, tenant_id: str, enabled: object, *, updated_by: str | None = None) -> ProxyConfig:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self.updated_by = updated_by
        self.cfg[tenant_id] = ProxyConfig(enabled=enabled, updated_at=NOW)
        return self.cfg[tenant_id]


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeProxyStore]]:
    with TestClient(app) as c:
        store = FakeProxyStore()
        app.state.tenant_proxy = store
        yield c, store


def test_get_defaults_off(client) -> None:
    c, _ = client
    r = c.get("/v1/tenant/proxy/config", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    assert r.json()["config"] == {"enabled": False, "updated_at": None}


def test_post_round_trip_records_who_changed_it(client) -> None:
    c, store = client
    r = c.post(
        "/v1/tenant/proxy/config",
        headers={"X-Tenant-Id": T, "X-Clerk-User-Id": "user_123"},
        json={"enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is True
    assert store.updated_by == "user_123"
    assert c.get("/v1/tenant/proxy/config", headers={"X-Tenant-Id": T}).json()["config"]["enabled"] is True


@pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 1}, {}, ["enabled"]])
def test_post_refuses_anything_but_a_boolean(client, body: object) -> None:
    c, store = client
    r = c.post("/v1/tenant/proxy/config", headers={"X-Tenant-Id": T}, json=body)
    assert r.status_code == 422
    assert store.get(T).enabled is False


# --- the watermark and its index must agree -------------------------------------------------------


def test_feed_watermark_matches_the_0033_index_expression() -> None:
    """If these drift, Postgres stops using the index and every proxy poll scans api_keys.

    0030 said the same thing in a comment. A comment did not stop 0033 needing a new index, so this
    time it is a test.
    """
    migration = (Path(__file__).resolve().parents[3] / "db" / "postgres" / "0033_tenant_proxy_config.sql").read_text()
    match = re.search(r"ON api_keys \(\s*(GREATEST\(.*?\)),\s*id\s*\)", migration, re.S)
    assert match, "could not find the watermark index expression in 0033"
    normalize = lambda sql: re.sub(r"\s+", "", sql)  # noqa: E731
    assert normalize(edge_keys._WATERMARK_SQL.replace("k.", "")) == normalize(match.group(1))
