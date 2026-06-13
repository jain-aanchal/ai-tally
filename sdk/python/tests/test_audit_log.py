# SPDX-License-Identifier: Apache-2.0
import dataclasses

import pytest

from tally.audit_log import (
    GENESIS_PREV_HASH,
    MAX_TOKEN_TTL_S,
    SEVEN_YEARS_S,
    AccessToken,
    AuditAction,
    AuditEntry,
    AuditLog,
    InMemoryAuditStore,
    TokenIssuer,
    VerificationResult,
)


class FakeClock:
    """Deterministic injectable clock."""

    def __init__(self, start: int = 1_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


def _log(clock: FakeClock | None = None) -> AuditLog:
    return AuditLog(InMemoryAuditStore(), clock=clock or FakeClock())


# --- recording + chain integrity ---


def test_first_entry_uses_genesis_prev_hash():
    log = _log()
    entry = log.record("alice", AuditAction.TENANT_CREATE, "t1")
    assert entry.sequence == 0
    assert entry.prev_hash == GENESIS_PREV_HASH


def test_each_entry_links_to_previous():
    log = _log()
    e0 = log.record("alice", AuditAction.TENANT_CREATE, "t1")
    e1 = log.record("bob", AuditAction.CONFIG_CHANGE, "t1")
    assert e1.prev_hash == e0.entry_hash
    assert e1.sequence == 1


def test_entry_hash_is_deterministic():
    e = AuditEntry(
        sequence=0,
        actor="alice",
        action=AuditAction.DELETION,
        tenant_id="t1",
        target="x",
        occurred_at_s=5,
        prev_hash=GENESIS_PREV_HASH,
        entry_hash="",
    )
    assert e.recompute_hash() == e.recompute_hash()


def test_clean_chain_verifies():
    log = _log()
    for i in range(10):
        log.record(f"user{i}", AuditAction.DATA_ACCESS, "t1", target=f"row{i}")
    result = log.verify()
    assert result.ok is True
    assert result.entries_checked == 10
    assert result.broken_at_sequence is None
    assert "OK" in result.summary()


def test_empty_log_verifies():
    assert _log().verify().ok is True


# --- tamper / reorder / gap detection ---


def test_detects_tampered_field():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=FakeClock())
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    log.record("bob", AuditAction.CONFIG_CHANGE, "t1")
    # Mutate the underlying list: replace entry 0's actor but keep its (now stale) hash.
    bad = dataclasses.replace(store._entries[0], actor="mallory")
    store._entries[0] = bad
    result = log.verify()
    assert result.ok is False
    assert result.broken_at_sequence == 0
    assert "tampered" in result.reason
    assert "BROKEN" in result.summary()


def test_detects_reorder():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=FakeClock())
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    log.record("bob", AuditAction.CONFIG_CHANGE, "t1")
    log.record("carol", AuditAction.KEY_ROTATION, "t1")
    store._entries[1], store._entries[2] = store._entries[2], store._entries[1]
    result = log.verify()
    assert result.ok is False
    assert result.broken_at_sequence is not None


def test_detects_sequence_gap():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=FakeClock())
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    log.record("bob", AuditAction.CONFIG_CHANGE, "t1")
    del store._entries[0]  # drop the genesis entry -> first remaining has sequence 1
    result = log.verify()
    assert result.ok is False
    assert "gap" in result.reason or "reorder" in result.reason


def test_detects_tamper_in_metadata():
    store = InMemoryAuditStore()
    log = AuditLog(store, clock=FakeClock())
    log.record("alice", AuditAction.CONFIG_CHANGE, "t1", metadata={"old": 1, "new": 2})
    bad = dataclasses.replace(store._entries[0], metadata={"old": 1, "new": 999})
    store._entries[0] = bad
    assert log.verify().ok is False


# --- query ---


def test_query_by_actor():
    clock = FakeClock()
    log = _log(clock)
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    log.record("bob", AuditAction.CONFIG_CHANGE, "t1")
    log.record("alice", AuditAction.DELETION, "t2")
    got = log.query(actor="alice")
    assert len(got) == 2
    assert all(e.actor == "alice" for e in got)


def test_query_by_action():
    log = _log()
    log.record("alice", AuditAction.DATA_ACCESS, "t1")
    log.record("bob", AuditAction.DATA_ACCESS, "t1")
    log.record("alice", AuditAction.DELETION, "t1")
    assert len(log.query(action=AuditAction.DATA_ACCESS)) == 2


def test_query_by_tenant():
    log = _log()
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    log.record("bob", AuditAction.TENANT_CREATE, "t2")
    assert len(log.query(tenant_id="t2")) == 1


def test_query_by_time_range_inclusive():
    log = _log()
    log.record("a", AuditAction.DATA_ACCESS, "t1", occurred_at_s=100)
    log.record("b", AuditAction.DATA_ACCESS, "t1", occurred_at_s=200)
    log.record("c", AuditAction.DATA_ACCESS, "t1", occurred_at_s=300)
    got = log.query(since_s=100, until_s=200)
    assert {e.actor for e in got} == {"a", "b"}


def test_query_combined_filters():
    log = _log()
    log.record("alice", AuditAction.DATA_ACCESS, "t1", occurred_at_s=10)
    log.record("alice", AuditAction.DATA_ACCESS, "t2", occurred_at_s=20)
    log.record("alice", AuditAction.DELETION, "t1", occurred_at_s=30)
    got = log.query(actor="alice", action=AuditAction.DATA_ACCESS, tenant_id="t1")
    assert len(got) == 1
    assert got[0].occurred_at_s == 10


def test_query_no_filters_returns_all():
    log = _log()
    log.record("a", AuditAction.DATA_ACCESS, "t1")
    log.record("b", AuditAction.DATA_ACCESS, "t1")
    assert len(log.query()) == 2


# --- retention (never deletes) ---


def test_retention_flags_old_entries():
    clock = FakeClock(start=0)
    log = _log(clock)
    log.record("a", AuditAction.DATA_ACCESS, "t1", occurred_at_s=0)
    log.record("b", AuditAction.DATA_ACCESS, "t1", occurred_at_s=SEVEN_YEARS_S + 10)
    report = log.retention_report(now_s=SEVEN_YEARS_S + 10)
    assert report.expired_sequences == (0,)


def test_retention_does_not_delete():
    log = _log()
    log.record("a", AuditAction.DATA_ACCESS, "t1", occurred_at_s=0)
    log.retention_report(now_s=SEVEN_YEARS_S * 2)
    assert len(log.entries()) == 1  # still there
    assert log.verify().ok is True


def test_retention_nothing_expired_when_recent():
    log = _log()
    log.record("a", AuditAction.DATA_ACCESS, "t1", occurred_at_s=1000)
    report = log.retention_report(now_s=2000)
    assert report.expired_sequences == ()


# --- validation / immutability ---


def test_record_rejects_empty_actor():
    with pytest.raises(ValueError):
        _log().record("", AuditAction.DELETION, "t1")


def test_record_rejects_empty_tenant():
    with pytest.raises(ValueError):
        _log().record("alice", AuditAction.DELETION, "")


def test_record_rejects_non_action():
    with pytest.raises(ValueError):
        _log().record("alice", "deletion", "t1")  # type: ignore[arg-type]


def test_entry_rejects_negative_sequence():
    with pytest.raises(ValueError):
        AuditEntry(
            sequence=-1,
            actor="a",
            action=AuditAction.DELETION,
            tenant_id="t1",
            target="",
            occurred_at_s=0,
            prev_hash=GENESIS_PREV_HASH,
            entry_hash="",
        )


def test_audit_entry_is_frozen():
    e = _log().record("alice", AuditAction.TENANT_CREATE, "t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.actor = "mallory"  # type: ignore[misc]


def test_entry_as_dict_roundtrips_fields():
    e = _log().record("alice", AuditAction.TENANT_CREATE, "t1", target="acme")
    d = e.as_dict()
    assert d["actor"] == "alice"
    assert d["action"] == "tenant_create"
    assert d["entry_hash"] == e.entry_hash


def test_verification_result_as_dict():
    r = VerificationResult(ok=True, entries_checked=3)
    assert r.as_dict()["ok"] is True


def test_store_protocol_satisfied():
    from tally.audit_log import AuditStore

    assert isinstance(InMemoryAuditStore(), AuditStore)


# --- access tokens ---


def test_issue_token_within_ttl():
    clock = FakeClock(start=1000)
    issuer = TokenIssuer(clock=clock)
    tok = issuer.issue("svc-reader", {"read:prod"}, ttl_s=600)
    assert tok.issued_at_s == 1000
    assert tok.expires_at_s == 1600
    assert tok.ttl_s == 600


def test_issue_rejects_ttl_over_one_hour():
    issuer = TokenIssuer(clock=FakeClock())
    with pytest.raises(ValueError):
        issuer.issue("svc", {"read:prod"}, ttl_s=MAX_TOKEN_TTL_S + 1)


def test_issue_rejects_non_positive_ttl():
    issuer = TokenIssuer(clock=FakeClock())
    with pytest.raises(ValueError):
        issuer.issue("svc", {"read:prod"}, ttl_s=0)
    with pytest.raises(ValueError):
        issuer.issue("svc", {"read:prod"}, ttl_s=-5)


def test_issuer_rejects_max_ttl_over_cap():
    with pytest.raises(ValueError):
        TokenIssuer(max_ttl_s=MAX_TOKEN_TTL_S + 1)


def test_issue_rejects_empty_subject():
    with pytest.raises(ValueError):
        TokenIssuer(clock=FakeClock()).issue("", {"read:prod"}, ttl_s=60)


def test_token_valid_before_expiry():
    clock = FakeClock(start=1000)
    tok = TokenIssuer(clock=clock).issue("svc", {"read:prod"}, ttl_s=600)
    assert tok.is_valid(1500) is True


def test_token_invalid_after_expiry():
    clock = FakeClock(start=1000)
    tok = TokenIssuer(clock=clock).issue("svc", {"read:prod"}, ttl_s=600)
    assert tok.is_expired(1600) is True
    assert tok.is_valid(1600) is False


def test_token_scope_check():
    clock = FakeClock(start=1000)
    tok = TokenIssuer(clock=clock).issue("svc", {"read:prod"}, ttl_s=600)
    assert tok.is_valid(1100, required_scope="read:prod") is True
    assert tok.is_valid(1100, required_scope="write:prod") is False


def test_token_rejects_inverted_window():
    with pytest.raises(ValueError):
        AccessToken(
            token_id="x",
            subject="svc",
            scopes=frozenset(),
            issued_at_s=100,
            expires_at_s=100,
        )


def test_token_is_frozen():
    tok = TokenIssuer(clock=FakeClock()).issue("svc", {"read:prod"}, ttl_s=60)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tok.subject = "other"  # type: ignore[misc]


def test_token_as_dict_sorts_scopes():
    tok = TokenIssuer(clock=FakeClock()).issue("svc", {"b", "a"}, ttl_s=60)
    assert tok.as_dict()["scopes"] == ["a", "b"]


# --- break-glass ---


def test_break_glass_issues_elevated_token():
    clock = FakeClock(start=1000)
    issuer = TokenIssuer(clock=clock)
    log = AuditLog(InMemoryAuditStore(), clock=clock)
    tok = issuer.break_glass("oncall", "prod outage debug", log)
    assert tok.elevated is True
    assert tok.ttl_s == MAX_TOKEN_TTL_S


def test_break_glass_writes_audit_entry():
    clock = FakeClock(start=1000)
    issuer = TokenIssuer(clock=clock)
    log = AuditLog(InMemoryAuditStore(), clock=clock)
    issuer.break_glass("oncall", "prod outage debug", log)
    entries = log.query(action=AuditAction.BREAK_GLASS)
    assert len(entries) == 1
    assert entries[0].actor == "oncall"
    assert entries[0].metadata["reason"] == "prod outage debug"


def test_break_glass_requires_reason():
    issuer = TokenIssuer(clock=FakeClock())
    log = AuditLog(InMemoryAuditStore(), clock=FakeClock())
    with pytest.raises(ValueError):
        issuer.break_glass("oncall", "", log)


def test_break_glass_keeps_chain_valid():
    clock = FakeClock(start=1000)
    issuer = TokenIssuer(clock=clock)
    log = AuditLog(InMemoryAuditStore(), clock=clock)
    log.record("alice", AuditAction.TENANT_CREATE, "t1")
    issuer.break_glass("oncall", "incident", log)
    assert log.verify().ok is True
