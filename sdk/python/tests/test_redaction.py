"""Tests for tally.redaction (CTO-76): redactors, payload policy, deletion plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tally.redaction import (
    DEFAULT_DELETION_SLA_DAYS,
    DELETION_TARGET_TABLES,
    DeletionRequest,
    InMemoryHmacKeyProvider,
    InMemoryTenantPolicyStore,
    PayloadMode,
    PayloadPolicy,
    PayloadPolicyEnforcer,
    Redactor,
    build_deletion_plan,
    hash_subject_id,
    hmac_hash,
)

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Redactor
# --------------------------------------------------------------------------- #
def test_redacts_email():
    r = Redactor()
    res = r.redact_text("contact me at jane.doe@example.com please")
    assert "jane.doe@example.com" not in res.text
    assert "[REDACTED:EMAIL]" in res.text
    assert res.findings["EMAIL"] == 1
    assert res.redacted is True
    assert res.total_findings == 1


def test_redacts_multiple_pii_types():
    r = Redactor()
    text = "email a@b.co ssn 123-45-6789 ip 10.0.0.1"
    res = r.redact_text(text)
    assert res.findings["EMAIL"] == 1
    assert res.findings["SSN"] == 1
    assert res.findings["IPV4"] == 1
    assert "a@b.co" not in res.text
    assert "123-45-6789" not in res.text
    assert "10.0.0.1" not in res.text


def test_redacts_credit_card_and_jwt_and_aws_key():
    r = Redactor()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
    res = r.redact_text(f"card 4111 1111 1111 1111 token {jwt} key AKIAIOSFODNN7EXAMPLE")
    assert res.findings.get("CREDIT_CARD") == 1
    assert res.findings.get("JWT") == 1
    assert res.findings.get("AWS_ACCESS_KEY") == 1


def test_no_pii_means_no_findings():
    r = Redactor()
    res = r.redact_text("the quick brown fox")
    assert res.text == "the quick brown fox"
    assert res.findings == {}
    assert res.redacted is False


def test_disable_detector():
    # Disable IPV4 (and PHONE, which also fuzzily matches dotted digit runs) and
    # confirm the IP literal survives untouched.
    r = Redactor(disable=["IPV4", "PHONE"])
    assert "IPV4" not in r.detector_names
    assert "PHONE" not in r.detector_names
    res = r.redact_text("ip 192.168.1.1")
    assert "192.168.1.1" in res.text  # not redacted
    assert "IPV4" not in res.findings


def test_custom_detector_set():
    import re

    from tally.redaction import Detector

    only_email = Detector("EMAIL", re.compile(r"\S+@\S+", re.IGNORECASE), "[X]")
    r = Redactor(detectors=[only_email])
    assert r.detector_names == ("EMAIL",)
    res = r.redact_text("a@b.com 123-45-6789")
    assert "a@b.com" not in res.text
    assert "123-45-6789" in res.text  # ssn detector not present


def test_non_string_and_none_never_raise():
    r = Redactor()
    assert r.redact_text(None).text == ""
    assert r.redact_text(None).findings == {}
    assert r.redact_text(12345).text == "12345"
    assert r.redact_text("").text == ""


def test_result_as_dict():
    r = Redactor()
    d = r.redact_text("a@b.co").as_dict()
    assert d["redacted"] is True
    assert d["total_findings"] == 1
    assert d["findings"] == {"EMAIL": 1}


# --------------------------------------------------------------------------- #
# HMAC hashing
# --------------------------------------------------------------------------- #
def test_hmac_hash_deterministic_and_keyed():
    assert hmac_hash("secret", b"k1") == hmac_hash("secret", b"k1")
    assert hmac_hash("secret", b"k1") != hmac_hash("secret", b"k2")
    assert len(hmac_hash("x", b"k")) == 64  # sha256 hex


def test_key_provider_per_tenant_distinct():
    p = InMemoryHmacKeyProvider(root_secret=b"root", key_version="v3")
    assert p.key_for("t1") != p.key_for("t2")
    assert p.key_for("t1") == p.key_for("t1")
    assert p.key_version("t1") == "v3"


# --------------------------------------------------------------------------- #
# Payload policy
# --------------------------------------------------------------------------- #
def _enforcer():
    return PayloadPolicyEnforcer(
        redactor=Redactor(),
        key_provider=InMemoryHmacKeyProvider(root_secret=b"fixed", key_version="v1"),
    )


def test_full_mode_redacts_content_keys_only():
    e = _enforcer()
    attrs = {"prompt": "email me a@b.co", "gen_ai.system": "openai"}
    res = e.apply("t1", attrs, PayloadPolicy(mode=PayloadMode.FULL))
    assert "a@b.co" not in res.attributes["prompt"]
    assert res.attributes["gen_ai.system"] == "openai"  # untouched
    assert res.findings["EMAIL"] == 1
    assert res.total_findings == 1


def test_full_mode_does_not_mutate_input():
    e = _enforcer()
    attrs = {"prompt": "a@b.co"}
    e.apply("t1", attrs, PayloadPolicy(mode=PayloadMode.FULL))
    assert attrs["prompt"] == "a@b.co"  # original intact


def test_hashed_mode_replaces_content_with_hmac():
    e = _enforcer()
    attrs = {"prompt": "sensitive text", "gen_ai.system": "openai"}
    res = e.apply("t1", attrs, PayloadPolicy(mode=PayloadMode.HASHED))
    assert res.attributes["prompt"].startswith("hmac:v1:")
    assert "sensitive text" not in res.attributes["prompt"]
    assert res.attributes["gen_ai.system"] == "openai"
    assert "prompt" in res.hashed_keys


def test_hashed_mode_is_tenant_scoped():
    e = _enforcer()
    pol = PayloadPolicy(mode=PayloadMode.HASHED)
    a = e.apply("t1", {"prompt": "x"}, pol).attributes["prompt"]
    b = e.apply("t2", {"prompt": "x"}, pol).attributes["prompt"]
    assert a != b  # same content, different tenant -> different hash


def test_none_mode_drops_with_marker():
    e = _enforcer()
    res = e.apply("t1", {"prompt": "secret", "x": 1}, PayloadPolicy(mode=PayloadMode.NONE))
    assert res.attributes["prompt"] == "[DROPPED]"
    assert res.attributes["x"] == 1
    assert "prompt" in res.dropped_keys


def test_none_mode_removes_key_when_marker_is_none():
    e = _enforcer()
    pol = PayloadPolicy(mode=PayloadMode.NONE, drop_marker=None)
    res = e.apply("t1", {"prompt": "secret"}, pol)
    assert "prompt" not in res.attributes
    assert "prompt" in res.dropped_keys


def test_missing_content_key_is_noop():
    e = _enforcer()
    res = e.apply("t1", {"gen_ai.system": "openai"}, PayloadPolicy(mode=PayloadMode.NONE))
    assert res.attributes == {"gen_ai.system": "openai"}
    assert res.dropped_keys == ()


def test_policy_application_as_dict():
    e = _enforcer()
    res = e.apply("t1", {"prompt": "a@b.co"}, PayloadPolicy(mode=PayloadMode.FULL))
    d = res.as_dict()
    assert d["mode"] == "full"
    assert d["total_findings"] == 1


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        PayloadPolicy(mode="full")  # type: ignore[arg-type]


def test_enforcer_zero_config():
    e = PayloadPolicyEnforcer()  # all defaults
    res = e.apply("t1", {"prompt": "a@b.co"}, PayloadPolicy())
    assert "a@b.co" not in res.attributes["prompt"]


# --------------------------------------------------------------------------- #
# Tenant policy store
# --------------------------------------------------------------------------- #
def test_policy_store_default_and_override():
    store = InMemoryTenantPolicyStore(default=PayloadPolicy(mode=PayloadMode.NONE))
    assert store.policy_for("unknown").mode is PayloadMode.NONE
    store.set_policy("t1", PayloadPolicy(mode=PayloadMode.HASHED))
    assert store.policy_for("t1").mode is PayloadMode.HASHED
    assert store.policy_for("t2").mode is PayloadMode.NONE


def test_policy_store_empty_tenant_raises():
    store = InMemoryTenantPolicyStore()
    with pytest.raises(ValueError):
        store.set_policy("", PayloadPolicy())


def test_policy_store_default_full():
    store = InMemoryTenantPolicyStore()
    assert store.policy_for("anyone").mode is PayloadMode.FULL


# --------------------------------------------------------------------------- #
# Right-to-deletion planning
# --------------------------------------------------------------------------- #
def test_hash_subject_id_deterministic():
    assert hash_subject_id("user-1") == hash_subject_id("user-1")
    assert hash_subject_id("user-1") != hash_subject_id("user-2")
    assert len(hash_subject_id("x")) == 64


def test_build_deletion_plan_hashes_and_sets_sla():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("user-1", "user-2"),
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    plan = build_deletion_plan(req)
    assert plan.tenant_id == "t1"
    assert plan.subject_count == 2
    # raw ids never appear
    assert "user-1" not in plan.hashed_subject_ids
    assert plan.hashed_subject_ids[0] == hash_subject_id("user-1")
    assert plan.target_tables == DELETION_TARGET_TABLES
    assert plan.sla_deadline == datetime(2026, 5, 1, tzinfo=UTC) + timedelta(
        days=DEFAULT_DELETION_SLA_DAYS
    )


def test_deletion_plan_dedups_subject_ids():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("a", "a", "b"),
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    plan = build_deletion_plan(req)
    assert plan.subject_count == 2


def test_deletion_plan_naive_received_at_treated_utc():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("a",),
        received_at=datetime(2026, 5, 1, 12, 0, 0),  # naive
    )
    plan = build_deletion_plan(req)
    assert plan.received_at.tzinfo is UTC


def test_deletion_plan_overdue():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("a",),
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    plan = build_deletion_plan(req, sla_days=30)
    assert plan.is_overdue(datetime(2026, 6, 5, tzinfo=UTC)) is True
    assert plan.is_overdue(datetime(2026, 5, 10, tzinfo=UTC)) is False


def test_deletion_plan_custom_tables_and_sla():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("a",),
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    plan = build_deletion_plan(req, target_tables=("otel_spans",), sla_days=7)
    assert plan.target_tables == ("otel_spans",)
    assert plan.sla_deadline == datetime(2026, 5, 8, tzinfo=UTC)


def test_deletion_plan_summary_and_as_dict():
    req = DeletionRequest(
        tenant_id="t1",
        subject_ids=("a", "b"),
        received_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    plan = build_deletion_plan(req)
    assert "t1" in plan.summary()
    assert "2 subject" in plan.summary()
    d = plan.as_dict()
    assert d["subject_count"] == 2
    assert d["tenant_id"] == "t1"
    assert "user" not in str(d)  # only hashes


def test_deletion_request_validation():
    with pytest.raises(ValueError):
        DeletionRequest(tenant_id="", subject_ids=("a",), received_at=datetime.now(UTC))
    with pytest.raises(ValueError):
        DeletionRequest(tenant_id="t1", subject_ids=(), received_at=datetime.now(UTC))


def test_build_plan_rejects_nonpositive_sla():
    req = DeletionRequest(
        tenant_id="t1", subject_ids=("a",), received_at=datetime(2026, 5, 1, tzinfo=UTC)
    )
    with pytest.raises(ValueError):
        build_deletion_plan(req, sla_days=0)
