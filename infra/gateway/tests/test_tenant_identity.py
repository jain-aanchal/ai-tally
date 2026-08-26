"""TenantIdentityResolver: tenant name <-> tenants.id UUID, fail-soft (CTO-185).

The gateway is handed a tenant NAME by the dashboard and a ``tenants.id`` UUID by API-key auth.
Anything HMAC'd treats that string as key material, so the two spellings are two key spaces. These
tests pin the expansion and, more importantly, pin that a control-plane outage degrades to the
caller's own spelling instead of failing the lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from gateway import tenant_identity
from gateway.tenant_identity import (
    FORM_NAME,
    FORM_UUID,
    TenantIdentityResolver,
    key_form,
)

NAME = "local-dev"
UUID_VALUE = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"


class _Settings:
    postgres_dsn = "postgresql://unused"


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_connect(monkeypatch: pytest.MonkeyPatch, row: tuple[Any, ...] | None) -> _FakeCursor:
    cursor = _FakeCursor(row)
    monkeypatch.setattr(
        tenant_identity.psycopg, "connect", lambda *_a, **_k: _FakeConn(cursor)
    )
    return cursor


def test_key_form_splits_on_uuid_parsing() -> None:
    assert key_form(UUID_VALUE) == FORM_UUID
    assert key_form(NAME) == FORM_NAME
    assert key_form("") == FORM_NAME


def test_name_expands_to_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _patch_connect(monkeypatch, (UUID_VALUE,))
    keys = TenantIdentityResolver(_Settings()).key_forms(NAME)
    assert [(k.value, k.form) for k in keys] == [(NAME, FORM_NAME), (UUID_VALUE, FORM_UUID)]
    assert "WHERE name" in cursor.executed[0][0]


def test_uuid_expands_to_name(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _patch_connect(monkeypatch, (NAME,))
    keys = TenantIdentityResolver(_Settings()).key_forms(UUID_VALUE)
    assert [(k.value, k.form) for k in keys] == [(UUID_VALUE, FORM_UUID), (NAME, FORM_NAME)]
    assert "WHERE id" in cursor.executed[0][0]


def test_unknown_tenant_yields_only_the_caller_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connect(monkeypatch, None)
    keys = TenantIdentityResolver(_Settings()).key_forms("not-a-tenant")
    assert [k.value for k in keys] == ["not-a-tenant"]


def test_postgres_failure_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(tenant_identity.psycopg, "connect", _boom)
    keys = TenantIdentityResolver(_Settings()).key_forms(NAME)
    assert [k.value for k in keys] == [NAME]


def test_failure_log_line_carries_only_the_exception_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("SELECT id FROM tenants WHERE name = 'leaky-value'")

    monkeypatch.setattr(tenant_identity.psycopg, "connect", _boom)
    with caplog.at_level("DEBUG"):
        TenantIdentityResolver(_Settings()).key_forms(NAME)
    assert "leaky-value" not in "\n".join(caplog.messages)


def test_empty_tenant_id_is_not_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not hit Postgres for an empty tenant id")

    monkeypatch.setattr(tenant_identity.psycopg, "connect", _boom)
    assert [k.value for k in TenantIdentityResolver(_Settings()).key_forms("")] == [""]
