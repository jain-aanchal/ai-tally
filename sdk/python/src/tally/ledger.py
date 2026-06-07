# SPDX-License-Identifier: Apache-2.0
"""Tamper-evident usage ledger + invoice export (CTO-87).

Billing has to be defensible. When a customer disputes an invoice we need to show, line by line,
exactly which usage was billed and prove the record was not altered after the fact. This module
is the append-only book of record that sits between *finalized usage* (whatever upstream metering
produced — this module is deliberately agnostic about how usage is measured) and the billing
integration (Stripe, CTO-90).

Three properties, each load-bearing:

- **Append-only + tamper-evident.** Every entry carries a hash chain: ``entry_hash = H(prev_hash
  || canonical(entry))``. Changing any past entry (amount, tenant, timestamp) breaks the chain
  from that point forward, and :meth:`UsageLedger.verify` localizes the first broken link. This is
  the same construction as a blockchain / Merkle log, minus distribution — one writer, one chain
  per tenant scope.
- **Reconcilable.** :meth:`UsageLedger.reconcile` compares ledger totals against an independent
  ingest count so we can detect dropped or duplicated usage before it reaches an invoice.
- **Idempotent export.** Each usage record carries an ``idempotency_key``; appending the same key
  twice is a no-op, and exporting an already-exported entry never bills it again. Re-running an
  export job is safe — no double-billing.

Money is integer micro-USD throughout (see :mod:`tally.schema`). Pure-Python, no infra: the
backing store is an injected :class:`LedgerStore` (in-memory by default) so dev/test never needs a
database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from tally.schema import DEFAULT_CURRENCY, micro_to_usd

#: Genesis link — the ``prev_hash`` of the very first entry in a chain.
GENESIS_HASH = "0" * 64


# --- inputs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """A finalized, billable unit of usage handed to the ledger.

    This is intentionally abstract: it is whatever upstream metering finalized. The ledger does not
    care how ``cost_micro_usd`` was computed, only that it is final and uniquely identified by
    ``idempotency_key`` (so re-submitting the same record is a no-op).
    """

    idempotency_key: str
    tenant_id: str
    cost_micro_usd: int
    occurred_at_ns: int
    feature_tag: str = ""
    quantity: int = 0
    unit: str = ""
    currency: str = DEFAULT_CURRENCY
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if isinstance(self.cost_micro_usd, bool) or not isinstance(self.cost_micro_usd, int):
            raise ValueError("cost_micro_usd must be an int (micro-USD)")
        if self.cost_micro_usd < 0:
            raise ValueError("cost_micro_usd must be >= 0")


# --- ledger entries -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable link in the hash chain.

    ``entry_hash`` is derived from ``prev_hash`` and the canonical payload; it is never set by a
    caller. ``sequence`` is the 0-based position in the tenant's chain.
    """

    sequence: int
    tenant_id: str
    idempotency_key: str
    cost_micro_usd: int
    occurred_at_ns: int
    appended_at_ns: int
    feature_tag: str
    currency: str
    prev_hash: str
    entry_hash: str
    exported: bool = False
    export_ref: str = ""

    def canonical_payload(self) -> str:
        """Deterministic, hash-input serialization of the billed fields (excludes the hashes
        themselves and the mutable export markers)."""
        return json.dumps(
            {
                "sequence": self.sequence,
                "tenant_id": self.tenant_id,
                "idempotency_key": self.idempotency_key,
                "cost_micro_usd": self.cost_micro_usd,
                "occurred_at_ns": self.occurred_at_ns,
                "appended_at_ns": self.appended_at_ns,
                "feature_tag": self.feature_tag,
                "currency": self.currency,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def recompute_hash(self) -> str:
        """Recompute ``entry_hash`` from the payload — used by :meth:`UsageLedger.verify`."""
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "tenant_id": self.tenant_id,
            "idempotency_key": self.idempotency_key,
            "cost_micro_usd": self.cost_micro_usd,
            "cost_usd": str(micro_to_usd(self.cost_micro_usd)),
            "occurred_at_ns": self.occurred_at_ns,
            "appended_at_ns": self.appended_at_ns,
            "feature_tag": self.feature_tag,
            "currency": self.currency,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
            "exported": self.exported,
            "export_ref": self.export_ref,
        }


# --- store --------------------------------------------------------------------------------------


@runtime_checkable
class LedgerStore(Protocol):
    """Persistence boundary. The default is in-memory; a production impl would back this with an
    append-only table. The ledger only ever appends and updates the export marker — it never
    deletes or rewrites billed fields."""

    def append(self, entry: LedgerEntry) -> None: ...

    def last(self, tenant_id: str) -> LedgerEntry | None: ...

    def entries(self, tenant_id: str) -> Sequence[LedgerEntry]: ...

    def has_key(self, tenant_id: str, idempotency_key: str) -> bool: ...

    def mark_exported(self, tenant_id: str, sequence: int, export_ref: str) -> None: ...


class InMemoryLedgerStore:
    """Default :class:`LedgerStore` — a per-tenant list. No infra; safe for dev/test."""

    __slots__ = ("_by_tenant", "_keys")

    def __init__(self) -> None:
        self._by_tenant: dict[str, list[LedgerEntry]] = {}
        self._keys: dict[str, set[str]] = {}

    def append(self, entry: LedgerEntry) -> None:
        self._by_tenant.setdefault(entry.tenant_id, []).append(entry)
        self._keys.setdefault(entry.tenant_id, set()).add(entry.idempotency_key)

    def last(self, tenant_id: str) -> LedgerEntry | None:
        chain = self._by_tenant.get(tenant_id)
        return chain[-1] if chain else None

    def entries(self, tenant_id: str) -> Sequence[LedgerEntry]:
        return tuple(self._by_tenant.get(tenant_id, ()))

    def has_key(self, tenant_id: str, idempotency_key: str) -> bool:
        return idempotency_key in self._keys.get(tenant_id, ())

    def mark_exported(self, tenant_id: str, sequence: int, export_ref: str) -> None:
        chain = self._by_tenant.get(tenant_id)
        if chain is None or not (0 <= sequence < len(chain)):
            return
        chain[sequence] = replace(chain[sequence], exported=True, export_ref=export_ref)


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a chain integrity check."""

    tenant_id: str
    ok: bool
    entries_checked: int
    broken_at_sequence: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "broken_at_sequence": self.broken_at_sequence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Ledger totals vs. an independent ingest count."""

    tenant_id: str
    ledger_count: int
    ingest_count: int
    ledger_total_micro_usd: int
    reconciled: bool

    @property
    def count_drift(self) -> int:
        """``ledger_count - ingest_count`` — positive means the ledger has extra (possible double
        count), negative means usage was dropped before reaching the ledger."""
        return self.ledger_count - self.ingest_count

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "ledger_count": self.ledger_count,
            "ingest_count": self.ingest_count,
            "count_drift": self.count_drift,
            "ledger_total_micro_usd": self.ledger_total_micro_usd,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """A single billable line on an exported invoice."""

    feature_tag: str
    quantity: int
    cost_micro_usd: int

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_tag": self.feature_tag,
            "quantity": self.quantity,
            "cost_micro_usd": self.cost_micro_usd,
            "cost_usd": str(micro_to_usd(self.cost_micro_usd)),
        }


@dataclass(frozen=True, slots=True)
class InvoiceExport:
    """An idempotent billing export — the shape Stripe (CTO-90) consumes.

    ``newly_exported_sequences`` are the entries this call actually marked as exported; on a re-run
    it is empty and ``total_micro_usd`` reflects only the (zero) new charges, so re-export never
    double-bills.
    """

    tenant_id: str
    export_ref: str
    currency: str
    total_micro_usd: int
    lines: tuple[InvoiceLine, ...]
    newly_exported_sequences: tuple[int, ...]
    entry_count: int

    @property
    def total_usd(self) -> str:
        return str(micro_to_usd(self.total_micro_usd))

    def summary(self) -> str:
        n = len(self.newly_exported_sequences)
        return (
            f"invoice {self.export_ref} for {self.tenant_id}: "
            f"${self.total_usd} {self.currency} across {self.entry_count} entries "
            f"({n} newly billed)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "export_ref": self.export_ref,
            "currency": self.currency,
            "total_micro_usd": self.total_micro_usd,
            "total_usd": self.total_usd,
            "lines": [line.as_dict() for line in self.lines],
            "newly_exported_sequences": list(self.newly_exported_sequences),
            "entry_count": self.entry_count,
            "summary": self.summary(),
        }


# --- ledger -------------------------------------------------------------------------------------


class UsageLedger:
    """Append-only, hash-chained, tamper-evident usage ledger.

    One hash chain per ``tenant_id``. Construct with an optional :class:`LedgerStore`; the default
    is in-memory so nothing external is required.
    """

    __slots__ = ("_store",)

    def __init__(self, store: LedgerStore | None = None) -> None:
        self._store = store if store is not None else InMemoryLedgerStore()

    # -- append --------------------------------------------------------------------------------

    def append(self, record: UsageRecord, *, appended_at_ns: int | None = None) -> LedgerEntry:
        """Append a finalized usage record, returning its (possibly pre-existing) ledger entry.

        Idempotent: if ``record.idempotency_key`` was already appended for this tenant, the
        original entry is returned unchanged and no new link is created.
        """
        existing = self._find_by_key(record.tenant_id, record.idempotency_key)
        if existing is not None:
            return existing

        prev = self._store.last(record.tenant_id)
        prev_hash = prev.entry_hash if prev is not None else GENESIS_HASH
        sequence = (prev.sequence + 1) if prev is not None else 0
        stamp = appended_at_ns if appended_at_ns is not None else record.occurred_at_ns

        draft = LedgerEntry(
            sequence=sequence,
            tenant_id=record.tenant_id,
            idempotency_key=record.idempotency_key,
            cost_micro_usd=record.cost_micro_usd,
            occurred_at_ns=record.occurred_at_ns,
            appended_at_ns=stamp,
            feature_tag=record.feature_tag,
            currency=record.currency,
            prev_hash=prev_hash,
            entry_hash="",
        )
        entry = replace(draft, entry_hash=draft.recompute_hash())
        self._store.append(entry)
        return entry

    def extend(
        self, records: Iterable[UsageRecord], *, appended_at_ns: int | None = None
    ) -> list[LedgerEntry]:
        """Append many records in order; skips non-:class:`UsageRecord` items defensively."""
        out: list[LedgerEntry] = []
        for record in records:
            if not isinstance(record, UsageRecord):
                continue
            out.append(self.append(record, appended_at_ns=appended_at_ns))
        return out

    # -- read ----------------------------------------------------------------------------------

    def entries(self, tenant_id: str) -> Sequence[LedgerEntry]:
        """All entries for a tenant, in chain order."""
        return self._store.entries(tenant_id)

    def total_micro_usd(self, tenant_id: str) -> int:
        return sum(e.cost_micro_usd for e in self._store.entries(tenant_id))

    def _find_by_key(self, tenant_id: str, idempotency_key: str) -> LedgerEntry | None:
        if not self._store.has_key(tenant_id, idempotency_key):
            return None
        for entry in self._store.entries(tenant_id):
            if entry.idempotency_key == idempotency_key:
                return entry
        return None

    # -- verify --------------------------------------------------------------------------------

    def verify(self, tenant_id: str) -> VerificationResult:
        """Walk the chain and confirm every link. Returns the first break, if any.

        A tamper (changed amount/tenant/timestamp) changes that entry's recomputed hash, so either
        its own ``entry_hash`` no longer matches or the *next* entry's ``prev_hash`` no longer
        matches — both are caught here.
        """
        entries = self._store.entries(tenant_id)
        expected_prev = GENESIS_HASH
        for i, entry in enumerate(entries):
            if entry.sequence != i:
                return VerificationResult(
                    tenant_id, False, i, i, f"sequence gap: expected {i}, got {entry.sequence}"
                )
            if entry.prev_hash != expected_prev:
                return VerificationResult(
                    tenant_id, False, i, i, "prev_hash does not match preceding entry_hash"
                )
            if entry.recompute_hash() != entry.entry_hash:
                return VerificationResult(
                    tenant_id, False, i, i, "entry_hash does not match payload (tampered)"
                )
            expected_prev = entry.entry_hash
        return VerificationResult(tenant_id, True, len(entries))

    # -- reconcile -----------------------------------------------------------------------------

    def reconcile(self, tenant_id: str, ingest_count: int) -> ReconciliationResult:
        """Compare the ledger entry count against an independent ingest count.

        ``reconciled`` is true only when the counts match exactly *and* the chain verifies — a
        broken chain can never be considered reconciled.
        """
        entries = self._store.entries(tenant_id)
        chain_ok = self.verify(tenant_id).ok
        return ReconciliationResult(
            tenant_id=tenant_id,
            ledger_count=len(entries),
            ingest_count=ingest_count,
            ledger_total_micro_usd=sum(e.cost_micro_usd for e in entries),
            reconciled=chain_ok and len(entries) == ingest_count,
        )

    # -- export --------------------------------------------------------------------------------

    def export_invoice(self, tenant_id: str, export_ref: str) -> InvoiceExport:
        """Export all not-yet-exported entries as an invoice and mark them exported.

        Idempotent on ``export_ref`` semantics: only entries still flagged ``exported=False`` are
        billed and marked; a second call with fresh entries bills only those, and a call with no
        new entries returns a zero-total invoice. Already-billed usage is never re-charged.
        """
        if not export_ref:
            raise ValueError("export_ref must be non-empty")

        entries = self._store.entries(tenant_id)
        pending = [e for e in entries if not e.exported]

        currency = pending[0].currency if pending else DEFAULT_CURRENCY
        by_feature: dict[str, list[int]] = {}
        total = 0
        newly: list[int] = []
        for entry in pending:
            total += entry.cost_micro_usd
            by_feature.setdefault(entry.feature_tag, [0, 0])
            bucket = by_feature[entry.feature_tag]
            bucket[0] += 1
            bucket[1] += entry.cost_micro_usd
            self._store.mark_exported(tenant_id, entry.sequence, export_ref)
            newly.append(entry.sequence)

        lines = tuple(
            InvoiceLine(feature_tag=tag, quantity=count, cost_micro_usd=cost)
            for tag, (count, cost) in sorted(
                by_feature.items(), key=lambda kv: kv[1][1], reverse=True
            )
        )
        return InvoiceExport(
            tenant_id=tenant_id,
            export_ref=export_ref,
            currency=currency,
            total_micro_usd=total,
            lines=lines,
            newly_exported_sequences=tuple(newly),
            entry_count=len(entries),
        )
