# SPDX-License-Identifier: Apache-2.0
"""Per-tenant price overrides: versioned + audited (CTO-54, spec §6).

Enterprise / committed-use customers negotiate custom provider rates; their cost must reflect their
*contract*, not list price. :mod:`tally.pricing` already supports per-tenant override entries that
take precedence over the public catalog (see :meth:`PriceCatalog.add_override` /
:meth:`PriceCatalog.lookup`). What it lacks, and what this module adds, is the **governance
layer**:

* **Versioned.** Every override slot ``(tenant_id, provider, model, price_type)`` carries a
  monotonic integer version. Re-pricing the same slot bumps the version and records which prior
  version it supersedes, so historical cost can be recomputed against the rate that was in force and
  a later correction never silently rewrites the past.
* **Audited.** Changes are an **append-only ledger**; nothing is ever mutated or deleted in
  place. Each entry captures *who* (``actor``), *why* (``reason``), and *when* (``recorded_at``,
  UTC). A revocation is a tombstone entry, not a deletion, so the audit trail is complete and
  tamper-evident by construction (append-only + monotonic versions).

The ledger is the source of truth; :meth:`OverrideLedger.apply_to_catalog` materializes the
*effective* overrides onto a freshly-seeded :class:`~tally.pricing.PriceCatalog` so the cost path
(:func:`tally.enrichment.enrich_cost`, which already threads ``tenant_id`` and the span's date)
picks them up with no further wiring. Effective means every window a slot still has, not just its
newest entry: the catalog resolves by date, so a rate negotiated in advance and the rate in force
today are both materialized and each span gets the one covering it. See
:meth:`OverrideLedger.effective`. Pure logic: no infra, no clock except an injectable ``now``
for deterministic tests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
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
    """Coerce a price to :class:`~decimal.Decimal`, never via float (this is money)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _to_unit(value: Unit | str) -> Unit:
    """Coerce ``unit`` to the enum, refusing an unknown spelling (CTO-416 review).

    Both enums here are ``str`` enums, so a plain string compares and hashes equal to its member and
    a caller passing one gets correct behaviour almost everywhere. Almost: ``.value`` then raises
    ``AttributeError``, and the first place that bit was inside the error message for a unit the
    cost math cannot apply, which turned a clean diagnosable failure into a mystery AttributeError
    raised while pricing a span. Coercing here means a record simply cannot be constructed with a
    non-enum unit, so no later reader has to be defensive. CTO-420 tracks the general trap.
    """
    if isinstance(value, Unit):
        return value
    try:
        return Unit(value)
    except (ValueError, TypeError) as exc:
        allowed = ", ".join(u.value for u in Unit)
        raise ValueError(f"unknown unit {value!r}; expected one of: {allowed}") from exc


def _to_price_type(value: PriceType | str) -> PriceType:
    """Coerce ``price_type`` to the enum, refusing an unknown spelling. See :func:`_to_unit`."""
    if isinstance(value, PriceType):
        return value
    try:
        return PriceType(value)
    except (ValueError, TypeError) as exc:
        allowed = ", ".join(t.value for t in PriceType)
        raise ValueError(f"unknown price_type {value!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    """One immutable, audited entry in the override ledger.

    A record either sets a rate (``price_per_unit`` is a :class:`~decimal.Decimal`) or *revokes* the
    slot (``price_per_unit is None``, a tombstone). Records are never mutated; a change appends a
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
    full history is retained for audit. :meth:`active` returns the latest live entry per slot (an
    audit view), :meth:`effective` returns every window that can still price a span, and
    :meth:`apply_to_catalog` materializes the latter onto a catalog.
    """

    def __init__(self, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._records: list[OverrideRecord] = []
        # latest version assigned per slot (monotonic; never reused even across revoke→re-add)
        self._version: dict[_Slot, int] = {}

    @classmethod
    def from_records(cls, records: Iterable[OverrideRecord]) -> OverrideLedger:
        """Rebuild a ledger from already-persisted records, oldest first (CTO-416).

        The gateway stores this ledger in Postgres (``price_catalog_overrides``), so the version and
        the ``supersedes`` back-reference are assigned by the WRITE, not re-derived here: several
        replicas append to one table and an in-memory counter per process would hand two of them the
        same version. This constructor therefore replays what was stored verbatim and only rebuilds
        the per-slot high-water mark, so :meth:`active` and :meth:`apply_to_catalog` behave exactly
        as they do for a ledger built in process.

        ``records`` MUST arrive in ledger order (ascending version within a slot), because
        :meth:`active` resolves a slot by last-write-wins. Feeding it a reversed cursor would
        resurrect a rate a tombstone already withdrew.
        """
        ledger = cls()
        for record in records:
            ledger._records.append(record)
            slot = record.slot
            ledger._version[slot] = max(ledger._version.get(slot, 0), record.version)
        return ledger

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

        ``price_per_unit`` is coerced to :class:`~decimal.Decimal` (accepts str/int), never a
        float, because this is money. ``actor`` and ``reason`` are required for the audit trail.
        """
        now = self._now()
        return self._append(
            tenant_id, provider, model, _to_price_type(price_type),
            unit=_to_unit(unit), price_per_unit=_to_decimal(price_per_unit),
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
        valid_from: date | None = None,
    ) -> OverrideRecord:
        """Revoke a tenant's override for a slot (records a tombstone; cost falls back to public).

        ``valid_from`` is the date the override ENDS, defaulting to today. It may be in the future
        ("this contract ends on 1 January") or in the past ("it ended on 1 August, filed today"),
        and :meth:`effective` resolves it against the span's date rather than applying it the moment
        it is filed: see that method for why a tombstone closes a window instead of erasing it.

        Idempotent in effect: revoking an already-absent/revoked slot still appends an audited
        tombstone (the request itself is part of the trail), but :meth:`active` will simply not
        surface the slot.
        """
        price_type = _to_price_type(price_type)
        prev = self._current(tenant_id, provider, model, price_type)
        unit = prev.unit if prev is not None else Unit.PER_MILLION_TOKENS
        currency = prev.currency if prev is not None else DEFAULT_CURRENCY
        now = self._now()
        return self._append(
            tenant_id, provider, model, price_type,
            unit=unit, price_per_unit=None,
            valid_from=valid_from or now.date(), valid_to=None,
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
        """The latest non-revoked record per slot, IGNORING the validity window.

        Answers "what was the last thing anyone said about this slot", which is what an audit view
        wants. It is NOT what prices a span, and must not be used to materialize a catalog: a rate
        negotiated in advance would hide the one in force today. See :meth:`effective`, which is
        what :meth:`apply_to_catalog` uses (CTO-416).

        Pass ``tenant_id`` to scope to one tenant. A slot whose latest record is a tombstone is
        omitted (it has fallen back to the public catalog).
        """
        latest: dict[_Slot, OverrideRecord] = {}
        for record in self._records:
            if tenant_id is not None and record.tenant_id != tenant_id:
                continue
            latest[record.slot] = record  # records are appended in order → last write wins
        return [r for r in latest.values() if not r.revoked]

    def effective(self, tenant_id: str | None = None) -> list[OverrideRecord]:
        """Every record that can still price something, with the DATE left to the catalog (CTO-416).

        This is what :meth:`apply_to_catalog` materializes, and it is deliberately not
        :meth:`active`. ``active`` collapses a slot to its latest record and ignores the validity
        window, which threw away the whole point of ``valid_from``:

        * A rate negotiated in advance (``valid_from`` next quarter) became the slot's only record,
          so the rate actually IN FORCE today was not materialized at all and the tenant silently
          priced at PUBLIC LIST until the new window opened. A contract rate is normally below list,
          so that over-reports spend that was never incurred.
        * A historical recompute hit the mirror image: a span from March resolved against a record
          that does not start until June, found nothing applicable, and came back unpriced.

        So every record a slot still has is handed to the catalog, and
        :meth:`tally.pricing.PriceCatalog._best` picks the one applicable at the span's date, which
        is the resolution it already implements for the public table.

        Two things change a record:

        * a LATER version with the SAME ``valid_from`` REPLACES it. That is a correction of one
          window rather than the opening of a new one.
        * a TOMBSTONE CLOSES the slot at its own ``valid_from`` rather than erasing it (CTO-416
          review). Every window that was open across that date has its ``valid_to`` moved to it, and
          any window that would start on or after it is removed, because the tombstone says there is
          no override from that date onward.

        Closing rather than deleting is what makes a DATED revocation mean what it says, in both
        directions, and it is the same resolution the rest of this module already relies on:

        * "this contract ends on 1 January 2027", filed today, must keep pricing at the contract
          rate until that date. Deleting the window instead started over-reporting that tenant at
          public list the moment the revocation was filed, months early, with the endpoint happily
          answering ``applied: true``.
        * "the contract ended on 1 August", filed on 1 September, must stop pricing on 1 August and
          must not leave a later window alive to resume afterwards.
        * a span from BEFORE the end date is still priced by the rate that was in force when the
          call was made. Deletion erased the contract from the past as well, so a backfill, a late
          arrival or a reconciliation rerun over a pre-revocation date came back at public list,
          which contradicts the promise that a past invoice stays explainable.

        A rate appended AFTER a tombstone re-opens the slot in the ordinary way, because the ledger
        is read in order and the last statement about a date wins.

        The ``valid_to`` a tombstone imposes is materialized onto the returned record, so what this
        returns is what the catalog should hold rather than a verbatim copy of the stored row. The
        stored rows themselves are untouched; :meth:`history` is the verbatim view.
        """
        windows: dict[_Slot, dict[date, OverrideRecord]] = {}
        for record in self._records:  # insertion order is version order
            if tenant_id is not None and record.tenant_id != tenant_id:
                continue
            window = windows.setdefault(record.slot, {})
            if record.revoked:
                ends_on = record.valid_from
                for start, open_record in list(window.items()):
                    if start >= ends_on:
                        # Nothing may start on or after the end date, including a window that was
                        # scheduled before this revocation was filed.
                        del window[start]
                    elif open_record.valid_to is None or open_record.valid_to > ends_on:
                        window[start] = replace(open_record, valid_to=ends_on)
                continue
            window[record.valid_from] = record
        return [
            record
            for slot in sorted(windows, key=lambda s: (s[0], s[1], s[2], s[3].value))
            for _start, record in sorted(windows[slot].items())
        ]

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
        """Materialize the EFFECTIVE overrides onto ``catalog`` (:meth:`PriceCatalog.add_override`).

        Every window a slot still has is materialized, not only the newest entry, so the catalog can
        resolve by the span's date the way it does for the public table: see :meth:`effective` for
        why materializing only the newest one priced a contract tenant at list.

        Intended for a *freshly seeded* catalog (the override entries are appended, so calling twice
        on the same catalog would double-register). Rebuild the catalog when the ledger changes.
        """
        for record in self.effective(tenant_id):
            entry = record.to_price_entry()
            if entry is not None:
                catalog.add_override(record.tenant_id, entry)
