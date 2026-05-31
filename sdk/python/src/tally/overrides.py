"""Per-tenant price overrides — versioned + audited (CTO-54, spec §6).

Enterprise / committed-use customers negotiate custom provider rates; their cost must reflect their
*contract*, not list price. :mod:`tally.pricing` already supports per-tenant override entries that
take precedence over the public catalog (see :meth:`PriceCatalog.add_override` /
:meth:`PriceCatalog.lookup`). What it lacks — and what this module adds — is the **governance
layer**:

* **Versioned.** Every override slot ``(tenant_id, provider, model, price_type)`` carries a
  monotonic integer version. Re-pricing the same slot bumps the version and records which prior
  version it supersedes, so historical cost can be recomputed against the rate that was in force and
  a later correction never silently rewrites the past.
* **Audited.** Changes are an **append-only ledger** — nothing is ever mutated or deleted in
  place. Each entry captures *who* (``actor``), *why* (``reason``), and *when* (``recorded_at``,
  UTC). A revocation is a tombstone entry, not a deletion, so the audit trail is complete and
  tamper-evident by construction (append-only + monotonic versions).

The ledger is the source of truth; :meth:`OverrideLedger.apply_to_catalog` materializes the
currently *active* overrides onto a freshly-seeded :class:`~tally.pricing.PriceCatalog` so the cost
path (:func:`tally.enrichment.enrich_cost`, which already threads ``tenant_id``) picks them up with
no further wiring. Pure logic — no infra, no clock except an injectable ``now`` for deterministic
tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from tally.pricing import PriceCatalog, PriceEntry, PriceType, Unit
from tally.schema import DEFAULT_CURRENCY

__all__ = [
    "OverrideRecord",
    "OverrideLedger",
]

# A slot is the unique target of an override: one rate for one tenant/provider/model/price_type.
_Slot = tuple[str, str, str, PriceType]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(value: Decimal | str | int) -> Decimal:
    """Coerce a price to :class:`~decimal.Decimal` — never via float (this is money)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    """One immutable, audited entry in the override ledger.

    A record either sets a rate (``price_per_unit`` is a :class:`~decimal.Decimal`) or *revokes* the
    slot (``price_per_unit is None`` — a tombstone). Records are never mutated; a change appends a
    new record with the next ``version`` for its slot and a ``supersedes`` back-reference.
    """

    tenant_id: str
    provider: str
    model: str
    price_type: PriceType
    version: int
    unit: Unit
    price_per_unit: Decimal | None
    valid_from: date
    valid_to: date | None
    actor: str
    reason: str
    recorded_at: datetime
    supersedes: int | None
    currency: str = DEFAULT_CURRENCY

    @property
    def slot(self) -> _Slot:
        return (self.tenant_id, self.provider, self.model, self.price_type)

    @property
    def revoked(self) -> bool:
        """True when this entry revokes the slot (a tombstone) rather than setting a rate."""
        return self.price_per_unit is None

    def to_price_entry(self) -> PriceEntry | None:
        """Project an active rate onto a :class:`~tally.pricing.PriceEntry`; ``None`` if revoked.

        The catalog version is the slot's audit version, stringified, so a span's recorded
        ``price_catalog_version`` distinguishes an override-priced cost from a public-catalog one.
        """
        if self.price_per_unit is None:
            return None
        return PriceEntry(
            version=f"override-{self.tenant_id}-v{self.version}",
            valid_from=self.valid_from,
            provider=self.provider,
            model=self.model,
            price_type=self.price_type,
            unit=self.unit,
            price_per_unit=self.price_per_unit,
            currency=self.currency,
            valid_to=self.valid_to,
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly audit view (Decimal/date/datetime rendered as strings)."""
        return {
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "model": self.model,
            "price_type": self.price_type.value,
            "version": self.version,
            "unit": self.unit.value,
            "price_per_unit": None if self.price_per_unit is None else str(self.price_per_unit),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": None if self.valid_to is None else self.valid_to.isoformat(),
            "actor": self.actor,
            "reason": self.reason,
            "recorded_at": self.recorded_at.isoformat(),
            "supersedes": self.supersedes,
            "currency": self.currency,
            "revoked": self.revoked,
        }


class OverrideLedger:
    """Append-only, versioned ledger of per-tenant price overrides.

    Mutations (:meth:`upsert`, :meth:`revoke`) only ever *append* a new :class:`OverrideRecord`; the
    full history is retained for audit. :meth:`active` returns the current effective overrides
    (latest non-revoked record per slot), and :meth:`apply_to_catalog` materializes them onto a
    catalog.
    """

    def __init__(self, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._records: list[OverrideRecord] = []
        # latest version assigned per slot (monotonic; never reused even across revoke→re-add)
        self._version: dict[_Slot, int] = {}

    # --- mutation (append-only) ------------------------------------------------------------------

    def upsert(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        price_type: PriceType,
        price_per_unit: Decimal | str | int,
        *,
        actor: str,
        reason: str,
        unit: Unit = Unit.PER_MILLION_TOKENS,
        valid_from: date | None = None,
        valid_to: date | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> OverrideRecord:
        """Set (or re-price) a tenant's override for a slot. Appends a new versioned record.

        ``price_per_unit`` is coerced to :class:`~decimal.Decimal` (accepts str/int) — never a
        float, because this is money. ``actor`` and ``reason`` are required for the audit trail.
        """
        now = self._now()
        return self._append(
            tenant_id, provider, model, price_type,
            unit=unit, price_per_unit=_to_decimal(price_per_unit),
            valid_from=valid_from or now.date(), valid_to=valid_to,
            actor=actor, reason=reason, currency=currency, now=now,
        )

    def revoke(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        price_type: PriceType,
        *,
        actor: str,
        reason: str,
    ) -> OverrideRecord:
        """Revoke a tenant's override for a slot (records a tombstone; cost falls back to public).

        Idempotent in effect: revoking an already-absent/revoked slot still appends an audited
        tombstone (the request itself is part of the trail), but :meth:`active` will simply not
        surface the slot.
        """
        prev = self._current(tenant_id, provider, model, price_type)
        unit = prev.unit if prev is not None else Unit.PER_MILLION_TOKENS
        currency = prev.currency if prev is not None else DEFAULT_CURRENCY
        now = self._now()
        return self._append(
            tenant_id, provider, model, price_type,
            unit=unit, price_per_unit=None,
            valid_from=now.date(), valid_to=None,
            actor=actor, reason=reason, currency=currency, now=now,
        )

    def _append(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        price_type: PriceType,
        *,
        unit: Unit,
        price_per_unit: Decimal | None,
        valid_from: date,
        valid_to: date | None,
        actor: str,
        reason: str,
        currency: str,
        now: datetime,
    ) -> OverrideRecord:
        slot: _Slot = (tenant_id, provider, model, price_type)
        prev_version = self._version.get(slot)
        version = (prev_version or 0) + 1
        record = OverrideRecord(
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            price_type=price_type,
            version=version,
            unit=unit,
            price_per_unit=price_per_unit,
            valid_from=valid_from,
            valid_to=valid_to,
            actor=actor,
            reason=reason,
            recorded_at=now,
            supersedes=prev_version,
            currency=currency,
        )
        self._records.append(record)
        self._version[slot] = version
        return record

    # --- query -----------------------------------------------------------------------------------

    def _current(
        self, tenant_id: str, provider: str, model: str, price_type: PriceType
    ) -> OverrideRecord | None:
        slot: _Slot = (tenant_id, provider, model, price_type)
        for record in reversed(self._records):
            if record.slot == slot:
                return record
        return None

    def current(
        self, tenant_id: str, provider: str, model: str, price_type: PriceType
    ) -> OverrideRecord | None:
        """Latest record for a slot (set *or* tombstone), or ``None`` if the slot is untouched."""
        return self._current(tenant_id, provider, model, price_type)

    def active(self, tenant_id: str | None = None) -> list[OverrideRecord]:
        """Currently effective overrides — latest non-revoked record per slot.

        Pass ``tenant_id`` to scope to one tenant. A slot whose latest record is a tombstone is
        omitted (it has fallen back to the public catalog).
        """
        latest: dict[_Slot, OverrideRecord] = {}
        for record in self._records:
            if tenant_id is not None and record.tenant_id != tenant_id:
                continue
            latest[record.slot] = record  # records are appended in order → last write wins
        return [r for r in latest.values() if not r.revoked]

    def history(
        self, tenant_id: str | None = None, *, slot: _Slot | None = None
    ) -> list[OverrideRecord]:
        """Append-only audit trail, in insertion order. Optionally filtered by tenant or slot."""
        out = self._records
        if tenant_id is not None:
            out = [r for r in out if r.tenant_id == tenant_id]
        if slot is not None:
            out = [r for r in out if r.slot == slot]
        return list(out)

    # --- integration -----------------------------------------------------------------------------

    def apply_to_catalog(self, catalog: PriceCatalog, *, tenant_id: str | None = None) -> None:
        """Materialize the active overrides onto ``catalog`` via :meth:`PriceCatalog.add_override`.

        Intended for a *freshly seeded* catalog (the override entries are appended, so calling twice
        on the same catalog would double-register). Rebuild the catalog when the ledger changes.
        """
        for record in self.active(tenant_id):
            entry = record.to_price_entry()
            if entry is not None:
                catalog.add_override(record.tenant_id, entry)
