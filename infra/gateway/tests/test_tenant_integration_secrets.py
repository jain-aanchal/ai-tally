"""Unit tests for the credential-by-reference layer (CTO-127).

Covers the parts that don't need Postgres: the dataclass, the env resolver, and the row decoder.
"""

from __future__ import annotations

import pytest

from gateway.tenant_integration_secrets import (
    ALLOWED_SECRET_CONNECTORS,
    EnvSecretResolver,
    IntegrationSecret,
    _row_to_secret,
)


def test_allowed_connectors_are_the_three_workers() -> None:
    assert ALLOWED_SECRET_CONNECTORS == {"segment", "hubspot", "pendo"}


def test_is_active_reflects_disconnected_at() -> None:
    live = IntegrationSecret("t", "segment", "ref", {}, "2026-01-01T00:00:00+00:00", None)
    gone = IntegrationSecret("t", "segment", "ref", {}, "2026-01-01T00:00:00+00:00", "2026-02-01")
    assert live.is_active is True
    assert gone.is_active is False


def test_env_resolver_reads_named_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SEGMENT_KEY", "wk_live_xyz")
    assert EnvSecretResolver().resolve("MY_SEGMENT_KEY") == "wk_live_xyz"


def test_env_resolver_raises_without_echoing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_REF", raising=False)
    with pytest.raises(KeyError) as exc:
        EnvSecretResolver().resolve("ABSENT_REF")
    # The reference name may appear; a resolved credential value must never be constructed.
    assert "ABSENT_REF" in str(exc.value)


def test_row_to_secret_parses_json_string_config() -> None:
    secret = _row_to_secret(
        ("t-1", "hubspot", "ref-2", '{"portal_id": "42"}', None, None)
    )
    assert secret.config == {"portal_id": "42"}
    assert secret.connector_id == "hubspot"
    assert secret.connected_at == ""  # non-datetime → empty string


def test_row_to_secret_accepts_dict_config() -> None:
    secret = _row_to_secret(("t-1", "pendo", "ref-3", {"region": "eu"}, None, None))
    assert secret.config == {"region": "eu"}
