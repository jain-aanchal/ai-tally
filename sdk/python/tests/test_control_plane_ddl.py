"""Structural validation for the Postgres control-plane schema (CTO-27).

No Postgres in CI; we assert the spec invariants: all required tables exist, secrets are KMS
references (not raw), tenant FKs cascade, and scope/mode constraints are present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DDL = Path(__file__).resolve().parents[3] / "db" / "postgres" / "0001_control_plane.sql"


@pytest.fixture(scope="module")
def ddl() -> str:
    assert DDL.exists(), f"missing {DDL}"
    return DDL.read_text()


@pytest.mark.parametrize(
    "table",
    [
        "tenants",
        "api_keys",
        "feature_tags",
        "value_events",
        "guardrails",
        "connector_configs",
        "plan_tiers",
        "usage_limits",
        "price_catalog_overrides",
    ],
)
def test_table_present(ddl, table):
    assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl


def test_secrets_are_kms_refs_not_raw(ddl):
    # tenants store a KEK reference; connectors store secret_kek_ref — never raw key columns
    assert "hash_salt_kek_ref" in ddl
    assert "secret_kek_ref" in ddl
    assert "no_raw_secret" in ddl  # CHECK guards against an obvious raw secret


def test_api_keys_store_hash_only(ddl):
    assert "key_hash" in ddl
    assert "scope IN ('read','write','admin')" in ddl


def test_tenant_fks_cascade(ddl):
    # deleting a tenant cascades (right-to-deletion hygiene)
    assert ddl.count("REFERENCES tenants(id) ON DELETE CASCADE") >= 6


def test_guardrail_modes_constrained(ddl):
    assert "mode IN ('observe','warn','graceful','hard_stop')" in ddl


def test_value_event_lookback_default_30(ddl):
    assert "lookback_days    INT NOT NULL DEFAULT 30" in ddl


def test_balanced_parens(ddl):
    assert ddl.count("(") == ddl.count(")")
