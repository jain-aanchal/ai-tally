# SPDX-License-Identifier: Apache-2.0
"""Tamper-evident usage ledger: hash chain, reconciliation, idempotent export (CTO-87)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tally.ledger import (
    GENESIS_HASH,
    InMemoryLedgerStore,
    InvoiceExport,
    LedgerEntry,
    ReconciliationResult,
    UsageLedger,
    UsageRecord,
    VerificationResult,
)


def _rec(key: str, *, tenant: str = "t1", cost: int = 1_000, feature: str = "chat",
         at: int = 1_000) -> UsageRecord:
    return UsageRecord(
        idempotency_key=key,
        tenant_id=tenant,
        cost_micro_usd=cost,
        occurred_at_ns=at,
        feature_tag=feature,
    )


# --- append + chain -----------------------------------------------------------------------------


def test_first_entry_chains_from_genesis() -> None:
    ledger = UsageLedger()
    entry = ledger.append(_rec("k1"))
    assert entry.sequence == 0
    assert entry.prev_hash == GENESIS_HASH
    assert entry.entry_hash == entry.recompute_hash()


def test_entries_chain_prev_to_entry_hash() -> None:
    ledger = UsageLedger()
    a = ledger.append(_rec("k1"))
    b = ledger.append(_rec("k2"))
    c = ledger.append(_rec("k3"))
    assert b.prev_hash == a.entry_hash
    assert c.prev_hash == b.entry_hash
    assert [e.sequence for e in (a, b, c)] == [0, 1, 2]


def test_chain_is_per_tenant() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1", tenant="t1"))
    other = ledger.append(_rec("k1", tenant="t2"))
    # independent chains: t2's first entry also starts from genesis
    assert other.sequence == 0
    assert other.prev_hash == GENESIS_HASH


# --- idempotency --------------------------------------------------------------------------------


def test_duplicate_idempotency_key_is_noop() -> None:
    ledger = UsageLedger()
    first = ledger.append(_rec("dup", cost=500))
    again = ledger.append(_rec("dup", cost=999))  # same key, different cost
    assert again == first  # original returned unchanged
    assert len(ledger.entries("t1")) == 1
    assert ledger.total_micro_usd("t1") == 500


def test_extend_appends_in_order_and_skips_garbage() -> None:
    ledger = UsageLedger()
    out = ledger.extend([_rec("k1"), "garbage", None, _rec("k2")])  # type: ignore[list-item]
    assert [e.idempotency_key for e in out] == ["k1", "k2"]
    assert len(ledger.entries("t1")) == 2


# --- verify / tamper-evidence -------------------------------------------------------------------


def test_verify_passes_for_clean_chain() -> None:
    ledger = UsageLedger()
    ledger.extend([_rec(f"k{i}") for i in range(5)])
    result = ledger.verify("t1")
    assert isinstance(result, VerificationResult)
    assert result.ok
    assert result.entries_checked == 5
    assert result.broken_at_sequence is None


def test_verify_detects_tampered_amount() -> None:
    store = InMemoryLedgerStore()
    ledger = UsageLedger(store)
    ledger.extend([_rec(f"k{i}", cost=1_000) for i in range(4)])
    # tamper: rewrite entry #1's cost without recomputing its hash
    chain = store._by_tenant["t1"]
    chain[1] = replace(chain[1], cost_micro_usd=999_999)
    result = ledger.verify("t1")
    assert not result.ok
    assert result.broken_at_sequence == 1


def test_verify_detects_reordering() -> None:
    store = InMemoryLedgerStore()
    ledger = UsageLedger(store)
    ledger.extend([_rec(f"k{i}") for i in range(3)])
    chain = store._by_tenant["t1"]
    chain[0], chain[1] = chain[1], chain[0]  # swap breaks sequence + prev_hash
    result = ledger.verify("t1")
    assert not result.ok
    assert result.broken_at_sequence == 0


def test_empty_chain_verifies() -> None:
    ledger = UsageLedger()
    assert ledger.verify("nobody").ok


# --- reconciliation -----------------------------------------------------------------------------


def test_reconcile_matches_ingest_count() -> None:
    ledger = UsageLedger()
    ledger.extend([_rec(f"k{i}", cost=2_000) for i in range(10)])
    result = ledger.reconcile("t1", ingest_count=10)
    assert isinstance(result, ReconciliationResult)
    assert result.reconciled
    assert result.count_drift == 0
    assert result.ledger_total_micro_usd == 20_000


def test_reconcile_flags_dropped_usage() -> None:
    ledger = UsageLedger()
    ledger.extend([_rec(f"k{i}") for i in range(8)])
    result = ledger.reconcile("t1", ingest_count=10)  # ingest saw 10, ledger has 8
    assert not result.reconciled
    assert result.count_drift == -2


def test_reconcile_fails_on_broken_chain_even_if_counts_match() -> None:
    store = InMemoryLedgerStore()
    ledger = UsageLedger(store)
    ledger.extend([_rec(f"k{i}") for i in range(5)])
    store._by_tenant["t1"][2] = replace(store._by_tenant["t1"][2], cost_micro_usd=7)
    result = ledger.reconcile("t1", ingest_count=5)
    assert result.count_drift == 0
    assert not result.reconciled  # chain integrity gates reconciliation


# --- export idempotency -------------------------------------------------------------------------


def test_export_bills_all_pending_entries() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1", cost=1_000, feature="chat"))
    ledger.append(_rec("k2", cost=3_000, feature="search"))
    invoice = ledger.export_invoice("t1", "inv-2026-05")
    assert isinstance(invoice, InvoiceExport)
    assert invoice.total_micro_usd == 4_000
    assert invoice.entry_count == 2
    assert set(invoice.newly_exported_sequences) == {0, 1}
    # lines sorted by cost desc
    assert invoice.lines[0].feature_tag == "search"
    assert invoice.lines[0].cost_micro_usd == 3_000


def test_re_export_does_not_double_bill() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1", cost=1_000))
    ledger.append(_rec("k2", cost=2_000))
    first = ledger.export_invoice("t1", "inv-1")
    second = ledger.export_invoice("t1", "inv-1-rerun")
    assert first.total_micro_usd == 3_000
    assert second.total_micro_usd == 0  # nothing new to bill
    assert second.newly_exported_sequences == ()


def test_export_then_new_usage_bills_only_the_new() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1", cost=1_000))
    ledger.export_invoice("t1", "inv-1")
    ledger.append(_rec("k2", cost=5_000))
    invoice = ledger.export_invoice("t1", "inv-2")
    assert invoice.total_micro_usd == 5_000
    assert invoice.newly_exported_sequences == (1,)
    assert invoice.entry_count == 2  # full history reported


def test_export_marks_entries_exported_with_ref() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1"))
    ledger.export_invoice("t1", "inv-xyz")
    entry = ledger.entries("t1")[0]
    assert entry.exported
    assert entry.export_ref == "inv-xyz"


def test_export_requires_ref() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1"))
    with pytest.raises(ValueError):
        ledger.export_invoice("t1", "")


def test_export_empty_tenant_is_zero_invoice() -> None:
    ledger = UsageLedger()
    invoice = ledger.export_invoice("nobody", "inv-0")
    assert invoice.total_micro_usd == 0
    assert invoice.lines == ()
    assert invoice.entry_count == 0


# --- export marking does not break the chain ----------------------------------------------------


def test_chain_still_verifies_after_export() -> None:
    ledger = UsageLedger()
    ledger.extend([_rec(f"k{i}") for i in range(4)])
    ledger.export_invoice("t1", "inv-1")
    # export mutates only the export markers, which are excluded from the hash payload
    assert ledger.verify("t1").ok


# --- validation ---------------------------------------------------------------------------------


def test_record_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        UsageRecord(idempotency_key="", tenant_id="t1", cost_micro_usd=1, occurred_at_ns=0)


def test_record_rejects_empty_tenant() -> None:
    with pytest.raises(ValueError):
        UsageRecord(idempotency_key="k", tenant_id="", cost_micro_usd=1, occurred_at_ns=0)


def test_record_rejects_negative_cost() -> None:
    with pytest.raises(ValueError):
        UsageRecord(idempotency_key="k", tenant_id="t1", cost_micro_usd=-1, occurred_at_ns=0)


def test_record_rejects_bool_cost() -> None:
    with pytest.raises(ValueError):
        UsageRecord(idempotency_key="k", tenant_id="t1", cost_micro_usd=True, occurred_at_ns=0)


# --- serialization --------------------------------------------------------------------------------


def test_entry_as_dict_round_trips_money() -> None:
    ledger = UsageLedger()
    entry = ledger.append(_rec("k1", cost=1_500_000))
    d = entry.as_dict()
    assert d["cost_micro_usd"] == 1_500_000
    assert d["cost_usd"] == "1.50000000"
    assert d["entry_hash"] == entry.entry_hash


def test_invoice_summary_and_as_dict() -> None:
    ledger = UsageLedger()
    ledger.append(_rec("k1", cost=2_500_000, feature="chat"))
    invoice = ledger.export_invoice("t1", "inv-9")
    s = invoice.summary()
    assert "inv-9" in s
    assert "2.50000000" in s
    d = invoice.as_dict()
    assert d["total_micro_usd"] == 2_500_000
    assert isinstance(d["lines"], list)
    assert "summary" in d


def test_types_are_frozen() -> None:
    assert LedgerEntry.__hash__ is not None
    assert UsageRecord.__hash__ is not None
    assert InvoiceExport.__hash__ is not None
