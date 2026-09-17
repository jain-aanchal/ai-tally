# SPDX-License-Identifier: Apache-2.0
"""Versioned + audited per-tenant price overrides (CTO-54)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tally.overrides import OverrideLedger, OverrideRecord
from tally.pricing import (
    PriceEntry,
    PriceType,
    Unit,
    Usage,
    _line,
    compute_cost_micro_usd,
    priceable_units,
    seed_catalog,
    unit_can_price,
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


# --- replay from storage + the fail-closed rule (CTO-416) ----------------------------------------


def test_from_records_replays_a_stored_ledger_verbatim() -> None:
    """The gateway stores this ledger in Postgres, so versions are assigned by the WRITE.

    Replaying must preserve them rather than renumbering, or the ``supersedes`` chain that explains
    a past invoice stops matching the rows it was written from.
    """
    source = _ledger()
    source.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="contract")
    source.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.5", actor="a", reason="renegotiated")
    stored = source.history()

    replayed = OverrideLedger.from_records(stored)
    assert [r.version for r in replayed.history()] == [1, 2]
    current = replayed.current("t", "openai", "gpt-5", PriceType.INPUT)
    assert current is not None and current.price_per_unit == Decimal("0.5")
    # The high-water mark is rebuilt, so a further append continues the chain instead of colliding.
    appended = replayed.upsert(
        "t", "openai", "gpt-5", PriceType.INPUT, "0.25", actor="a", reason="again"
    )
    assert (appended.version, appended.supersedes) == (3, 2)


def test_from_records_carries_tombstones_through() -> None:
    source = _ledger()
    source.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="contract")
    source.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended")
    replayed = OverrideLedger.from_records(source.history())
    assert replayed.active("t") == []


def test_overrides_unavailable_prices_nothing_for_a_tenant_rather_than_list_price() -> None:
    """CTO-416 honesty invariant, at the layer that enforces it.

    A contract rate is usually BELOW list, so answering the public rate while the ledger is
    unreadable reports spend the customer never incurred, in a figure nothing downstream can tell
    apart from a real one. An unknown must stay unknown.
    """
    cat = seed_catalog()
    at = date(2026, 5, 15)
    usage = Usage(input_tokens=1_000_000)
    priced, version = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")
    assert priced == 2_500_000 and version

    cat.mark_overrides_unavailable("postgres unreachable")
    _blank, blank_version = compute_cost_micro_usd(
        cat, "openai", "gpt-5", usage, at=at, tenant_id="t"
    )
    # An empty version is what tally.enrichment turns into NULL / CostSource 'unpriced'.
    assert blank_version == ""
    assert cat.overrides_unavailable == "postgres unreachable"

    cat.mark_overrides_loaded()
    recovered = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")
    assert recovered == (priced, version)


def test_a_lookup_with_no_tenant_still_answers_while_overrides_are_unavailable() -> None:
    """The fail-closed rule is about a TENANT's cost, not about every internal estimate.

    A lookup that names no tenant cannot be standing in for a contract rate, so an unreadable ledger
    says nothing about it and it keeps answering from the public catalog.
    """
    cat = seed_catalog()
    cat.mark_overrides_unavailable("postgres unreachable")
    usage = Usage(input_tokens=1_000_000)
    cost, version = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 5, 15))
    assert cost == 2_500_000 and version


def test_clear_overrides_drops_the_pool_in_place() -> None:
    """The reload path mutates one catalog object; callers hold a reference to it."""
    cat = seed_catalog()
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="contract")
    led.apply_to_catalog(cat)
    at = date(2026, 5, 15)
    usage = Usage(input_tokens=1_000_000)
    contract, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")
    assert contract == 1_000_000
    cat.clear_overrides()
    public, _ = compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")
    assert public == 2_500_000


# --- date windows and unusable units (CTO-416 review) --------------------------------------------


def test_a_rate_negotiated_in_advance_does_not_hide_the_one_in_force() -> None:
    """``active`` collapses a slot to its newest record, which is NOT what may price a span.

    Materializing only that record left the rate actually in force today out of the catalog, so the
    lookup fell through to the public list price, and a contract rate is normally well below list.
    ``effective`` hands every surviving window to the catalog and lets it resolve by date, which is
    what ``valid_from`` was for in the first place.
    """
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="in force",
               valid_from=date(2026, 1, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.04", actor="a", reason="next quarter",
               valid_from=date(2026, 12, 1))

    assert [str(r.price_per_unit) for r in led.active("t")] == ["0.04"]
    assert sorted(str(r.price_per_unit) for r in led.effective("t")) == ["0.04", "0.05"]

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)
    today, later = date(2026, 6, 15), date(2026, 12, 15)

    def cost(at: date) -> int:
        return compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")[0]

    assert cost(today) == 50_000
    assert cost(later) == 40_000
    assert cost(today) != 2_500_000  # and emphatically not the public 2.50 per million


def test_a_historical_span_is_priced_by_the_window_that_was_in_force() -> None:
    """The mirror image: a March span must not resolve against a rate that starts in June."""
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="q1",
               valid_from=date(2026, 1, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.04", actor="a", reason="q2",
               valid_from=date(2026, 6, 1))
    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)

    def cost(at: date) -> int:
        return compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")[0]

    assert cost(date(2026, 3, 15)) == 50_000
    assert cost(date(2026, 7, 15)) == 40_000


def test_a_correction_to_one_window_replaces_only_that_window() -> None:
    """A later version with the SAME valid_from is a correction, not a new window."""
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="q1",
               valid_from=date(2026, 1, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.04", actor="a", reason="q2",
               valid_from=date(2026, 6, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.055", actor="a", reason="q1 was wrong",
               valid_from=date(2026, 1, 1))
    assert sorted(str(r.price_per_unit) for r in led.effective("t")) == ["0.04", "0.055"]


def test_a_tombstone_closes_the_slot_at_its_own_date_rather_than_erasing_it() -> None:
    """A revocation is resolved against the SPAN's date, not applied the moment it is filed.

    Deleting the windows outright had three consequences, and this covers all three: a dated
    revocation took effect immediately rather than on its date, a window scheduled before the
    revocation survived it forever, and the contract vanished from the PAST so a recompute of a
    pre-revocation span came back at public list.
    """
    led = OverrideLedger(now=_Clock(datetime(2026, 5, 1, 12, tzinfo=timezone.utc)))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="current",
               valid_from=date(2026, 1, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.04", actor="a", reason="scheduled",
               valid_from=date(2026, 12, 1))
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="contract ends",
               valid_from=date(2026, 9, 1))

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)

    def cost(at: date) -> int:
        return compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")[0]

    assert cost(date(2026, 3, 1)) == 50_000  # history stays priceable at the contract rate
    assert cost(date(2026, 8, 31)) == 50_000  # right up to the end date
    assert cost(date(2026, 9, 1)) == 2_500_000  # and from the end date, public list
    # The window scheduled for December was scheduled BEFORE the revocation, and the revocation says
    # there is no override from September onward. It must not resume.
    assert cost(date(2026, 12, 15)) == 2_500_000

    # The closed window is reported with the end date the tombstone gave it.
    closed = [r for r in led.effective("t")]
    assert [(r.valid_from, r.valid_to) for r in closed] == [(date(2026, 1, 1), date(2026, 9, 1))]


def test_a_revocation_filed_in_advance_does_not_reprice_today() -> None:
    """The reported case: "this contract ends on 1 January 2027", filed months earlier.

    The endpoint answered applied: true and the tenant silently started over-reporting at public
    list from the moment the revocation was filed.
    """
    led = OverrideLedger(now=_Clock(datetime(2026, 8, 1, 12, tzinfo=timezone.utc)))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="contract",
               valid_from=date(2026, 7, 1))
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ends next year",
               valid_from=date(2027, 1, 1))

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 8, 1),
                                  tenant_id="t")[0] == 50_000
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2027, 1, 2),
                                  tenant_id="t")[0] == 2_500_000


def test_a_backdated_revocation_ends_the_override_where_it_says() -> None:
    """The mirror case: "the contract ended on 1 August", filed on 1 September."""
    led = OverrideLedger(now=_Clock(datetime(2026, 9, 1, 12, tzinfo=timezone.utc)))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="contract",
               valid_from=date(2026, 1, 1))
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended in August",
               valid_from=date(2026, 8, 1))

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 7, 31),
                                  tenant_id="t")[0] == 50_000
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 8, 2),
                                  tenant_id="t")[0] == 2_500_000


def test_a_rate_appended_after_a_tombstone_reopens_the_slot() -> None:
    """The ledger is read in order: the last statement about a date wins."""
    led = OverrideLedger(now=_Clock(datetime(2026, 9, 1, 12, tzinfo=timezone.utc)))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="old contract",
               valid_from=date(2026, 1, 1))
    led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended",
               valid_from=date(2026, 9, 1))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.03", actor="a", reason="renewed",
               valid_from=date(2027, 1, 1))

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)

    def cost(at: date) -> int:
        return compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=at, tenant_id="t")[0]

    assert cost(date(2026, 5, 1)) == 50_000  # the old contract, before it ended
    assert cost(date(2026, 10, 1)) == 2_500_000  # the gap between contracts
    assert cost(date(2027, 2, 1)) == 30_000  # the renewal


def test_a_revocation_defaults_to_today_when_no_date_is_given() -> None:
    """The plain "stop overriding" case keeps working, and still leaves history priceable."""
    led = OverrideLedger(now=_Clock(datetime(2026, 9, 1, 12, tzinfo=timezone.utc)))
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="contract",
               valid_from=date(2026, 1, 1))
    tombstone = led.revoke("t", "openai", "gpt-5", PriceType.INPUT, actor="a", reason="ended")
    assert tombstone.valid_from == date(2026, 9, 1)

    cat = seed_catalog()
    led.apply_to_catalog(cat)
    usage = Usage(input_tokens=1_000_000)
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 6, 1),
                                  tenant_id="t")[0] == 50_000
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 9, 2),
                                  tenant_id="t")[0] == 2_500_000


def test_a_unit_that_cannot_price_its_tier_is_a_miss_not_a_confident_zero() -> None:
    """A per-GB rate on a token tier used to price every matching span at exactly 0.

    ``_line`` had no branch for it and returned Decimal(0), and because a catalog version WAS set
    the span read as priced rather than as unpriced. The public rate still applies here, which is
    the point: the unusable entry is skipped rather than honoured.
    """
    led = _ledger()
    led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="bad unit",
               unit=Unit.PER_GB)
    cat = seed_catalog()
    led.apply_to_catalog(cat)
    cost, version = compute_cost_micro_usd(
        cat, "openai", "gpt-5", Usage(input_tokens=1_000_000), at=date(2026, 6, 15), tenant_id="t"
    )
    assert cost == 2_500_000  # the public rate, not 0
    assert version == "seed-2026-06-15"
    assert not unit_can_price(PriceType.INPUT, Unit.PER_GB)
    assert priceable_units(PriceType.TOOL_CALL) == ["per_call"]


def test_the_cost_math_raises_rather_than_returning_zero_for_a_unit_it_cannot_apply() -> None:
    """The backstop that keeps the next unit added to the enum from reintroducing the zero."""
    entry = PriceEntry(
        version="v1",
        valid_from=date(2026, 1, 1),
        provider="openai",
        model="gpt-5",
        price_type=PriceType.INPUT,
        unit=Unit.PER_GB,
        price_per_unit=Decimal("1"),
    )
    with pytest.raises(ValueError, match="no cost arithmetic"):
        _line(entry, 1_000_000)


def test_replace_overrides_installs_a_pool_and_clears_the_fail_closed_flag() -> None:
    """One assignment, so a concurrent reader never sees a half-built pool (CTO-416 review)."""
    cat = seed_catalog()
    cat.mark_overrides_unavailable("postgres unreachable")
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="contract")
    entry = rec.to_price_entry()
    assert entry is not None
    cat.replace_overrides({"t": [entry]})
    assert cat.overrides_unavailable is None
    usage = Usage(input_tokens=1_000_000)
    assert compute_cost_micro_usd(cat, "openai", "gpt-5", usage, at=date(2026, 6, 15),
                                  tenant_id="t")[0] == 1_000_000


def test_marking_unavailable_drops_the_pool_in_the_same_call() -> None:
    """Flag first, pool second: the reverse order leaves an empty pool still looking usable."""
    cat = seed_catalog()
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1.0", actor="a", reason="contract")
    entry = rec.to_price_entry()
    assert entry is not None
    cat.replace_overrides({"t": [entry]})
    cat.mark_overrides_unavailable("postgres unreachable")
    assert cat.overrides_unavailable == "postgres unreachable"
    assert (
        cat.lookup("openai", "gpt-5", PriceType.INPUT, at=date(2026, 6, 15), tenant_id="t") is None
    )


# --- a non-enum unit must not turn a clean failure into an AttributeError (CTO-416 review) --------


def test_a_string_unit_is_coerced_by_the_public_upsert() -> None:
    """Both enums here are str enums, so a string mostly works, right up until something calls
    ``.value`` on it. Coercing at the boundary means such a record cannot exist."""
    led = _ledger()
    rec = led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "0.05", actor="a", reason="r",
                     unit="per_call")
    assert rec.unit is Unit.PER_CALL
    assert rec.to_price_entry().unit is Unit.PER_CALL

    typed = led.upsert("t", "openai", "gpt-5", "output", "0.05", actor="a", reason="r")
    assert typed.price_type is PriceType.OUTPUT
    assert typed.as_dict()["price_type"] == "output"  # the call that used to raise


def test_an_unknown_unit_or_tier_is_refused_rather_than_stored() -> None:
    led = _ledger()
    with pytest.raises(ValueError, match="unknown unit"):
        led.upsert("t", "openai", "gpt-5", PriceType.INPUT, "1", actor="a", reason="r",
                   unit="per_fortnight")
    with pytest.raises(ValueError, match="unknown price_type"):
        led.upsert("t", "openai", "gpt-5", "wholesale", "1", actor="a", reason="r")


def test_the_unpriceable_unit_error_survives_a_non_enum_entry() -> None:
    """The error message itself must not raise while reporting the error.

    A PriceEntry built outside the ledger can still carry plain strings, and the AttributeError that
    came out of formatting this message was raised from inside ``enrich_cost`` on the ingest path,
    which is a different and much worse failure mode than the ValueError it was trying to raise.
    """
    entry = PriceEntry(
        version="v1",
        valid_from=date(2026, 1, 1),
        provider="openai",
        model="gpt-5",
        price_type="input",  # type: ignore[arg-type]
        unit="per_gb",  # type: ignore[arg-type]
        price_per_unit=Decimal("1"),
    )
    with pytest.raises(ValueError, match="no cost arithmetic for unit per_gb"):
        _line(entry, 1_000_000)
