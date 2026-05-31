"""Tests for zero-touch tenant bootstrap (CTO-88).

These assert the spec invariants without any Postgres/KMS: a signup deterministically plans the
right control-plane rows, secrets are never raw, provisioning is idempotent, and freshly-minted
tenants are isolated from each other.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tally.provisioning import (
    API_KEY_PREFIX,
    DEFAULT_GUARDRAIL_MODE,
    DEFAULT_PLAN,
    ApiKeyIssue,
    HmacKeyRef,
    Region,
    Scope,
    SignupRequest,
    TenantRegistry,
    provision_tenant,
    verify_isolation,
)

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)


def _request(org: str = "Acme Inc", email: str = "admin@acme.com", region: Region = Region.US_EAST):
    return SignupRequest(org_name=org, admin_email=email, region=region)


# --- SignupRequest validation ---------------------------------------------------------


def test_valid_request_constructs():
    req = _request()
    assert req.region is Region.US_EAST


@pytest.mark.parametrize("org", ["", "   ", "x" * 201])
def test_bad_org_rejected(org):
    with pytest.raises(ValueError):
        _request(org=org)


@pytest.mark.parametrize("email", ["", "no-at", "two@@at.com", "a@nodot", "@acme.com", "a@"])
def test_bad_email_rejected(email):
    with pytest.raises(ValueError):
        _request(email=email)


def test_region_must_be_enum():
    with pytest.raises(ValueError):
        SignupRequest(org_name="Acme", admin_email="a@b.com", region="us-east")  # type: ignore[arg-type]


def test_normalization_is_case_and_whitespace_insensitive():
    a = _request(org="  Acme   Inc ", email="Admin@Acme.com")
    b = _request(org="acme inc", email="admin@acme.com")
    assert a.idempotency_key == b.idempotency_key


def test_idempotency_key_changes_with_region():
    us = _request(region=Region.US_EAST)
    eu = _request(region=Region.EU_WEST)
    assert us.idempotency_key != eu.idempotency_key


# --- Region --------------------------------------------------------------------------------------


def test_region_residency_and_host():
    assert Region.EU_WEST.residency == "EU"
    assert Region.EU_WEST.ingest_host == "ingest.eu-west.ai-tally.dev"


# --- API key minting ------------------------------------------------------------------


def test_minted_key_hash_matches_raw_and_prefix():
    issue = ApiKeyIssue.mint(Scope.WRITE, token="deadbeef")
    assert issue.raw == f"{API_KEY_PREFIX}deadbeef"
    # hash is sha256 of the raw token, 64 hex chars
    assert len(issue.key_hash) == 64
    assert issue.key_hash != issue.raw
    assert issue.display_prefix.startswith(API_KEY_PREFIX)


def test_minted_keys_are_unique_without_injection():
    a = ApiKeyIssue.mint()
    b = ApiKeyIssue.mint()
    assert a.raw != b.raw
    assert a.key_hash != b.key_hash


# --- HmacKeyRef -----------------------------------------------------------------------


def test_hmac_ref_is_namespaced_and_versioned():
    ref = HmacKeyRef.for_tenant("t_123", version=2)
    assert "/tenant/t_123/" in ref.kek_ref
    assert ref.version == 2


@pytest.mark.parametrize("bad", ["sk-rawsecretmaterial", "x" * 512])
def test_hmac_ref_rejects_rawlike_material(bad):
    with pytest.raises(ValueError):
        HmacKeyRef(kek_ref=bad, version=1)


def test_hmac_ref_rejects_bad_version():
    with pytest.raises(ValueError):
        HmacKeyRef(kek_ref="kms://x", version=0)


# --- provision_tenant -----------------------------------------------------------------


def test_provision_plans_all_rows():
    res = provision_tenant(_request(), now=NOW, tenant_id="t_abc", api_token="tok")
    plan = res.plan
    assert plan.tenant_row.id == "t_abc"
    assert plan.tenant_row.name == "Acme Inc"
    assert plan.tenant_row.region == "us-east"
    assert plan.tenant_row.plan == DEFAULT_PLAN
    assert plan.api_key_row.tenant_id == "t_abc"
    assert plan.api_key_row.scope == "write"
    assert plan.hmac_key.kek_ref == plan.tenant_row.hash_salt_kek_ref
    assert res.reused is False


def test_default_config_is_observe_and_full_sample():
    plan = provision_tenant(_request(), now=NOW).plan
    assert plan.default_config["guardrail_mode"] == DEFAULT_GUARDRAIL_MODE
    assert plan.default_config["sample_rate"] == 1.0
    assert plan.default_config["residency"] == "US"


def test_raw_key_returned_once_and_not_in_plan():
    res = provision_tenant(_request(), now=NOW, tenant_id="t_x", api_token="secret-body")
    # The raw key is on the result, only as a hash on the plan.
    assert res.api_key.raw == f"{API_KEY_PREFIX}secret-body"
    serialized = res.plan.as_dict()
    flat = repr(serialized)
    assert "secret-body" not in flat
    assert res.api_key.key_hash in flat  # the hash *is* stored


def test_plan_assert_no_raw_secret_passes_for_clean_plan():
    plan = provision_tenant(_request(), now=NOW).plan
    plan.assert_no_raw_secret()  # should not raise


def test_first_trace_env_uses_region_host_and_raw_key():
    res = provision_tenant(_request(region=Region.EU_WEST), now=NOW, api_token="tok")
    env = res.first_trace_env
    assert env["OPENAI_BASE_URL"] == "https://ingest.eu-west.ai-tally.dev/v1"
    assert env["TALLY_TENANT_KEY"] == f"{API_KEY_PREFIX}tok"


def test_ingest_credentials_carry_no_raw_key():
    plan = provision_tenant(_request(), now=NOW, api_token="tok").plan
    creds = plan.ingest_credentials()
    assert "tok" not in repr(creds)
    assert creds["ingest_host"] == "ingest.us-east.ai-tally.dev"


def test_admin_scope_propagates():
    plan = provision_tenant(_request(), now=NOW, scope=Scope.ADMIN).plan
    assert plan.api_key_row.scope == "admin"


def test_provision_defaults_generate_random_ids_and_keys():
    a = provision_tenant(_request())
    b = provision_tenant(_request())
    assert a.plan.tenant_id != b.plan.tenant_id
    assert a.api_key.raw != b.api_key.raw


# --- TenantRegistry idempotency -------------------------------------------------------


def test_registry_provisions_once_then_replays():
    reg = TenantRegistry()
    first = reg.provision(_request(), now=NOW, tenant_id="t_1", api_token="tok")
    second = reg.provision(_request(), now=NOW, tenant_id="t_2", api_token="tok2")
    assert first.reused is False
    assert second.reused is True
    # Same tenant returned; the second tenant_id/token were never used.
    assert second.plan.tenant_id == "t_1"
    assert len(reg) == 1


def test_registry_replay_has_no_raw_key():
    reg = TenantRegistry()
    reg.provision(_request(), now=NOW, tenant_id="t_1", api_token="tok")
    replay = reg.provision(_request(), now=NOW)
    assert replay.reused is True
    assert replay.api_key.raw == ""  # nothing to re-show; original was delivered once


def test_registry_distinct_signups_create_distinct_tenants():
    reg = TenantRegistry()
    reg.provision(_request(org="Acme"), now=NOW, tenant_id="t_1")
    reg.provision(_request(org="Globex"), now=NOW, tenant_id="t_2")
    assert len(reg) == 2


def test_registry_rejects_id_collision():
    reg = TenantRegistry()
    reg.provision(_request(org="Acme"), now=NOW, tenant_id="dup")
    with pytest.raises(ValueError):
        reg.provision(_request(org="Globex"), now=NOW, tenant_id="dup")


# --- Isolation verification -----------------------------------------------------------


def test_isolation_holds_for_fresh_tenants():
    plans = [
        provision_tenant(_request(org="A"), now=NOW, tenant_id="t_a", api_token="a").plan,
        provision_tenant(_request(org="B"), now=NOW, tenant_id="t_b", api_token="b").plan,
        provision_tenant(_request(org="C"), now=NOW, tenant_id="t_c", api_token="c").plan,
    ]
    assert verify_isolation(plans) == []


def test_isolation_flags_shared_key_hash():
    # Two tenants minted with the SAME token → same key hash → isolation violation.
    plans = [
        provision_tenant(_request(org="A"), now=NOW, tenant_id="t_a", api_token="same").plan,
        provision_tenant(_request(org="B"), now=NOW, tenant_id="t_b", api_token="same").plan,
    ]
    violations = verify_isolation(plans)
    assert any("api key hash" in v for v in violations)


def test_isolation_flags_duplicate_tenant_id():
    plans = [
        provision_tenant(_request(org="A"), now=NOW, tenant_id="dup", api_token="a").plan,
        provision_tenant(_request(org="B"), now=NOW, tenant_id="dup", api_token="b").plan,
    ]
    violations = verify_isolation(plans)
    assert any("duplicate tenant_id" in v for v in violations)
