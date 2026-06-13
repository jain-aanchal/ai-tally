# SPDX-License-Identifier: Apache-2.0
"""Versioned + audited per-tenant price overrides (CTO-54)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tally.overrides import OverrideLedger, OverrideRecord
from tally.pricing import (
    PriceType,
    Unit,
    Usage,
    compute_cost_micro_usd,
    seed_catalog,
)


class _Clock:
    """Deterministic, monotonic injectable clock."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        t = self._t
        self._t = t.replace(microsecond=(t.microsecond + 1))
        return t


def _ledger(*, when: datetime | None = None) -> OverrideLedger:
    return OverrideLedger(now=_Clock(when or datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)))


# --- versioning ----------------------------------------------------------------------------------


def test_first_override_is_version_1_with_no_supersedes() -> None:
    led = _ledger()
    rec = led.upsert("tenant_a", "openai", "gpt-5", PriceType.INPUT, "1.00",
                     actor="alice", reason="committed-use contract")
    assert rec.version == 1
    assert rec.supersedes is None
    assert rec.price_per_unit == Decimal("1.00")


def test_repricing_same_slot_bumps_version_and_links_supersedes() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="v1")
    v2 = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.80", actor="b", reason="renego")
    assert v2.version == 2
    assert v2.supersedes == 1
    assert led.current("t", "openai", "gpt-5", PriceType.INPUT).price_per_unit == Decimal("0.80")


def test_versions_are_monotonic_across_revoke_and_readd() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="v1")
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="contract ended")
    readd = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.90", actor="a", reason="renewed")
    # never reused: v1 set, v2 tombstone, v3 set
    assert readd.version == 3
    assert readd.supersedes == 2


def test_distinct_slots_version_independently() -> None:
    led = _ledger()
    a = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="r")
    b = led.upsert("t", "openai", "gpt-5", PriceType.OUTPUT, "5.0", actor="a", reason="r")
    assert a.version == 1 and b.version == 1  # separate slots, separate counters


# --- money is Decimal, never float ---------------------------------------------------------------


def test_price_coerced_to_decimal_not_float() -> None:
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, 2, actor="a", reason="int input")
    assert isinstance(rec.price_per_unit, Decimal)
    assert rec.price_per_unit == Decimal("2")


# --- revoke is a tombstone, not a deletion -------------------------------------------------------


def test_revoke_records_tombstone_and_drops_from_active() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="v1")
    tomb = led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended")
    assert tomb.revoked is True
    assert tomb.price_per_unit is None
    assert led.active("t") == []  # no longer effective
    # but the trail is intact
    assert len(led.history("t")) == 2


def test_active_returns_latest_nonrevoked_per_slot() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="v1")
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.50", actor="a", reason="v2")
    active = led.active("t")
    assert len(active) == 1
    assert active[0].price_per_unit == Decimal("0.50")
    assert active[0].version == 2


# --- audit trail ---------------------------------------------------------------------------------


def test_history_is_append_only_and_ordered() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="first")
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.9", actor="b", reason="second")
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="c", reason="third")
    hist = led.history("t")
    assert [r.version for r in hist] == [1, 2, 3]
    assert [r.actor for r in hist] == ["a", "b", "c"]
    assert [r.reason for r in hist] == ["first", "second", "third"]


def test_audit_fields_captured_who_why_when() -> None:
    ts = datetime(2026, 6, 1, 9, 30, 0, tzinfo=timezone.utc)
    led = _ledger(when=ts)
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0",
                     actor="cfo@acme.test", reason="Q3 committed-use amendment")
    assert rec.actor == "cfo@acme.test"
    assert rec.reason == "Q3 committed-use amendment"
    assert rec.recorded_at == ts


def test_record_as_dict_is_json_friendly() -> None:
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.25", actor="a", reason="r")
    d = rec.as_dict()
    assert d["price_per_unit"] == "1.25"  # Decimal rendered as string
    assert d["price_type"] == "input"
    assert d["revoked"] is False
    assert isinstance(d["recorded_at"], str)


# --- tenant scoping ------------------------------------------------------------------------------


def test_active_scopes_by_tenant() -> None:
    led = _ledger()
    led.upsert("tenant_a", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="r")
    led.upsert("tenant_b", "openai", "gpt-5", PriceType.INPUT, "2.0", actor="a", reason="r")
    assert {r.tenant_id for r in led.active("tenant_a")} == {"tenant_a"}
    assert len(led.active()) == 2  # unscoped sees both


# --- validity window -----------------------------------------------------------------------------


def test_valid_from_defaults_to_now_and_window_honored() -> None:
    ts = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    led = _ledger(when=ts)
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="r")
    assert rec.valid_from == date(2026, 7, 1)
    entry = rec.to_price_entry()
    assert entry is not None
    assert entry.is_valid_at(date(2026, 7, 1))
    assert not entry.is_valid_at(date(2026, 6, 30))


def test_revoked_record_has_no_price_entry() -> None:
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="r")
    tomb = led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="r")
    assert tomb.to_price_entry() is None


# --- integration: override precedence in the real cost path --------------------------------------


def test_override_takes_precedence_over_public_catalog_in_cost() -> None:
    cat = seed_catalog()
    led = _ledger()
    # public gpt-5 input is 2.50/Mtok; this tenant negotiated 1.00.
    rec = led.upsert("vip", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="deal")
    led.apply_to_catalog(cat)

    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    at = date(2026, 5, 15)
    vip_cost, _ = compute_cost_micro_usd(
        cat, "openai", "gpt-5", usage, at=at, tenant_id="vip"
    )
    public_cost, _ = compute_cost_micro_usd(
        cat, "openai", "gpt-5", usage, at=at, tenant_id="other"
    )
    assert vip_cost < public_cost  # override is cheaper
    assert vip_cost == 1_000_000  # 1.00 USD == 1_000_000 micro-USD for 1M tokens
    # the materialized override entry carries the audit version, distinguishing it from list price.
    entry = rec.to_price_entry()
    assert entry is not None and entry.version == "override-vip-v1"


def test_apply_to_catalog_skips_revoked_so_cost_falls_back() -> None:
    cat = seed_catalog()
    led = _ledger()
    led.upsert("vip", "openai", "gpt-5", PriceType.INPUT, "1.00", actor="a", reason="c")
    led.revoke("vip", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended")
    led.apply_to_catalog(cat)

    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    at = date(2026, 5, 15)
    vip_cost, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="vip")
    public_cost, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="x")
    assert vip_cost == public_cost  # revoked → back to list price


def test_apply_to_catalog_can_scope_to_one_tenant() -> None:
    cat = seed_catalog()
    led = _ledger()
    led.upsert("a", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="x", reason="r")
    led.upsert("b", "openai", "gpt-5", PriceType.INPUT, "0.5", actor="x", reason="r")
    led.apply_to_catalog(cat, tenant_id="b")
    at = date(2026, 5, 15)
    usage = Usage(input_tokens=1_000_000)
    # tenant a not applied → public 2.50; tenant b applied → 0.50
    a_cost, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="a")
    b_cost, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="b")
    assert a_cost == 2_500_000
    assert b_cost == 500_000


# --- record immutability -------------------------------------------------------------------------


def test_override_record_is_frozen() -> None:
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="r")
    with pytest.raises(FrozenInstanceError):
        rec.price_per_unit = Decimal("9.99")  # type: ignore[misc]


def test_unit_per_call_override_supported() -> None:
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.TOOL_CALL, "0.01",
                     actor="a", reason="tool pricing", unit=Unit.PER_CALL)
    assert isinstance(rec, OverrideRecord)
    entry = rec.to_price_entry()
    assert entry is not None and entry.unit is Unit.PER_CALL
