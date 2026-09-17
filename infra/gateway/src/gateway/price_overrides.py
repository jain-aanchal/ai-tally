# SPDX-License-Identifier: Apache-2.0
"""Per-tenant price override ledger, wired into the running gateway (CTO-416).

WHY this exists. Every piece of the override machinery was already written and tested and NOTHING
connected it. ``price_catalog_overrides`` has been in the schema since migration 0001,
:class:`tally.overrides.OverrideLedger` (CTO-54) is a versioned, append-only, audited ledger with an
``apply_to_catalog`` method, and :meth:`tally.pricing.PriceCatalog.lookup` already consults a
tenant's overrides ahead of the public pools. But the gateway built its catalog from
``seed_catalog()`` once at startup and no endpoint ever wrote a row, so a missing or negotiated
price meant editing ``sdk/python/src/tally/pricing.py`` and shipping a release. This module is the
wiring: load the ledger onto the catalog at startup, refresh it without a restart, and expose the
control-plane read/append endpoints that make a price change a write rather than a deploy.

THE HONESTY RULE, which is the whole reason this module is more than twenty lines.
-------------------------------------------------------------------------------
If the ledger cannot be read, the gateway does NOT price from the public catalog. A negotiated rate
is almost always BELOW list, so falling back would report spend the customer never incurred, and it
would look exactly like a real figure: nothing downstream, and no human reading the dashboard, could
tell the difference. So a failed load calls
:meth:`~tally.pricing.PriceCatalog.mark_overrides_unavailable` and every tenant-scoped lookup then
answers ``None``, which :func:`tally.enrichment.enrich_cost` turns into a NULL cost with
``CostSource = 'unpriced'``: the same honest blank this codebase renders everywhere else it does not
know. The failure is loud, not swallowed: an ERROR log at the point of failure, and
:meth:`PriceOverrideRefresher.status` feeds a ``price_overrides`` check on ``/readyz`` so a monitor
sees it without reading logs.

Note what that costs, stated plainly: while the ledger is unreadable, LLM spans go unpriced for
every tenant, not only for the tenants that actually hold an override. That is deliberate. Knowing
which tenants hold one requires reading the very table we just failed to read, so the conservative
answer is the only honest one, and it self-heals on the next successful refresh.

WHY IT IS OPT-IN (``TALLY_PRICE_OVERRIDES_ENABLED``, default off). Same reasoning as
``scheduler_enabled`` and ``ingest_buffered``: with the flag off nothing here runs and behaviour is
byte-identical to before this landed, so a checkout with no Postgres (and the test suite) boots and
prices exactly as it did. With the flag on, the strictness above applies in full. The flag is
environment configuration, so turning it on, and every price change after that, is still a write
rather than a release. ``infra/docker-compose.yml`` ships it on for the local stack.

THE REFRESH PATH. Three triggers, no new machinery:

* **startup**, in the lifespan, so a replica never serves traffic priced from a catalog it has not
  reconciled with the ledger;
* **write-through**, right after a successful append, so the operator who just added a price sees it
  applied on the replica that took the write;
* **a short TTL on the ingest path** (``price_overrides_refresh_ttl_s``), which is what carries a
  write to the OTHER replicas. This follows ``usage_cache_ttl_s`` (CTO-390), the existing precedent
  for "bounded staleness in front of a durable source", rather than inventing a pub/sub channel.
  There is also an explicit ``POST /v1/tenant/price-overrides/refresh`` for an operator who does not
  want to wait out the TTL.

MONEY. Rates are :class:`~decimal.Decimal` from the moment they leave the JSON body (parsed from a
string, never through float) all the way to ``NUMERIC(20,8)``. The API takes a rate per unit, which
is what a contract states; the micro-USD integer is computed downstream by the pricing layer, which
is the only place money becomes a number to add up.

NO SECRETS. A price is a contract term. Nothing here accepts, stores or references key material.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import errors as pg_errors

from tally.overrides import OverrideLedger, OverrideRecord
from tally.pricing import PriceCatalog, PriceType, Unit
from tally.schema import DEFAULT_CURRENCY

from gateway.config import Settings
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

logger = logging.getLogger("tally.gateway.price_overrides")

#: Bounds mirroring the CHECK constraints in migration 0035. Validated here so a caller gets a 422
#: naming the field rather than a 503 carrying a constraint name.
MAX_ACTOR_CHARS = 200
MAX_REASON_CHARS = 500
MAX_PROVIDER_CHARS = 100
MAX_MODEL_CHARS = 200

#: Typo guard on a rate, not a business rule. Rates are per million tokens (or per call / per GB),
#: so anything above this is a misplaced decimal rather than a contract, and a silent extra three
#: zeros would make every cost figure for that tenant meaningless while looking plausible.
MAX_PRICE_PER_UNIT = Decimal("100000")


class PriceOverrideError(ValueError):
    """Caller-facing validation error. Surfaces as HTTP 422."""


class PriceOverrideConflict(PriceOverrideError):
    """Another writer appended the same slot version first. Surfaces as HTTP 409."""


#: Re-exported so an endpoint catches one name for "no such tenant", as the budgets store does.
TenantNotFound = TenantNotFoundError


# --- validation ----------------------------------------------------------------------------------


def _normalize_text(value: object, *, field: str, max_chars: int, lower: bool = False) -> str:
    if not isinstance(value, str):
        raise PriceOverrideError(f"{field} must be a string")
    trimmed = value.strip()
    if lower:
        trimmed = trimmed.lower()
    if not trimmed:
        raise PriceOverrideError(f"{field} must be non-empty")
    if len(trimmed) > max_chars:
        raise PriceOverrideError(f"{field} must be at most {max_chars} characters")
    return trimmed


def normalize_provider(value: object) -> str:
    """Provider id, lowercased because that is how the catalog and the spans spell it."""
    return _normalize_text(value, field="provider", max_chars=MAX_PROVIDER_CHARS, lower=True)


def normalize_model(value: object) -> str:
    """Model id, case PRESERVED: it is matched against telemetry that preserves case, so folding it
    here would produce an override that prices nothing and reports no error."""
    return _normalize_text(value, field="model", max_chars=MAX_MODEL_CHARS)


def normalize_price_type(value: object) -> PriceType:
    """One of the catalog's real price tiers.

    An unknown tier is refused rather than coerced to ``input``. Storing a rate against a tier the
    cost path never looks up is an override that silently does nothing, which is indistinguishable
    from one that works until an invoice disagrees.
    """
    if isinstance(value, PriceType):
        return value
    raw = _normalize_text(value, field="price_type", max_chars=64, lower=True)
    try:
        return PriceType(raw)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in PriceType)
        raise PriceOverrideError(f"price_type must be one of: {allowed}") from exc


def normalize_unit(value: object) -> Unit:
    """The unit the rate is quoted in. Defaults to per-million-tokens, as the catalog does."""
    if value is None:
        return Unit.PER_MILLION_TOKENS
    if isinstance(value, Unit):
        return value
    raw = _normalize_text(value, field="unit", max_chars=64, lower=True)
    try:
        return Unit(raw)
    except ValueError as exc:
        allowed = ", ".join(u.value for u in Unit)
        raise PriceOverrideError(f"unit must be one of: {allowed}") from exc


def normalize_price_per_unit(value: object) -> Decimal:
    """Parse a contract rate into :class:`~decimal.Decimal`, refusing float outright.

    A float is a 422 even when it looks harmless. ``0.07`` is not 0.07 in binary, and money that
    starts life as a float has already lost precision before it reaches the NUMERIC column. A string
    ("0.07") or an integer is the accepted shape, which is also what a contract document says.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise PriceOverrideError(
            "price_per_unit must be a decimal STRING (e.g. \"0.07\") or an integer, never a float"
        )
    if isinstance(value, Decimal):
        rate = value
    elif isinstance(value, int):
        rate = Decimal(value)
    elif isinstance(value, str):
        try:
            rate = Decimal(value.strip())
        except (InvalidOperation, ArithmeticError) as exc:
            raise PriceOverrideError("price_per_unit is not a decimal number") from exc
    else:
        raise PriceOverrideError("price_per_unit must be a decimal string or an integer")
    if not rate.is_finite():
        raise PriceOverrideError("price_per_unit must be a finite number")
    if rate < 0:
        raise PriceOverrideError("price_per_unit must be >= 0")
    if rate > MAX_PRICE_PER_UNIT:
        raise PriceOverrideError(
            f"price_per_unit must be at most {MAX_PRICE_PER_UNIT}; check for a misplaced decimal"
        )
    return rate


def normalize_actor(value: object) -> str:
    """WHO made this change. Required: an audit entry without an actor is not an audit entry."""
    return _normalize_text(value, field="actor", max_chars=MAX_ACTOR_CHARS)


def normalize_reason(value: object) -> str:
    """WHY the rate changed. Required for the same reason ``actor`` is.

    This is the field that answers "why is this tenant's cost different from list?" a year from now,
    when the person who negotiated the contract has left.
    """
    return _normalize_text(value, field="reason", max_chars=MAX_REASON_CHARS)


def normalize_optional_date(value: object, *, field: str) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        raise PriceOverrideError(f"{field} must be a date (YYYY-MM-DD), not a timestamp")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise PriceOverrideError(f"{field} must be an ISO date string (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise PriceOverrideError(f"{field} must be an ISO date string (YYYY-MM-DD)") from exc


def normalize_currency(value: object) -> str:
    if value is None:
        return DEFAULT_CURRENCY
    raw = _normalize_text(value, field="currency", max_chars=8)
    return raw.upper()


# --- storage -------------------------------------------------------------------------------------

_COLUMNS = (
    "tenant_id, provider, model, price_type, version, unit, price_per_unit, "
    "valid_from, valid_to, actor, reason, recorded_at, supersedes, currency"
)


def unambiguous_spellings(rows: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Keep only the alternate tenant spellings that identify exactly ONE tenant.

    Pure, so the rule can be tested without a database. See
    :meth:`PriceOverrideStore.load_tenant_spellings` for why an ambiguous name must not carry an
    override: two tenants may share a ``name``, and pricing the second one's spans from the first
    one's contract is a cross-tenant leak of exactly the kind this ticket is about.
    """
    claims: dict[str, int] = {}
    for _canonical, alternates in rows:
        for alias in set(alternates):
            claims[alias] = claims.get(alias, 0) + 1
    return {
        canonical: [a for a in alternates if a != canonical and claims.get(a, 0) == 1]
        for canonical, alternates in rows
    }


def _row_to_record(row: tuple) -> OverrideRecord:
    """Project one stored row onto the SDK's ledger record.

    ``price_per_unit`` arrives from psycopg as a Decimal (NUMERIC), and stays one: this is the money
    boundary and nothing here converts through float.
    """
    return OverrideRecord(
        tenant_id=str(row[0]),
        provider=str(row[1]),
        model=str(row[2]),
        price_type=PriceType(str(row[3])),
        version=int(row[4]),
        unit=Unit(str(row[5])),
        price_per_unit=None if row[6] is None else Decimal(row[6]),
        valid_from=row[7],
        valid_to=row[8],
        actor=str(row[9]),
        reason=str(row[10]),
        recorded_at=row[11],
        supersedes=None if row[12] is None else int(row[12]),
        currency=str(row[13]),
    )


class PriceOverrideStore:
    """Postgres-backed, APPEND-ONLY access to ``price_catalog_overrides``.

    There is deliberately no update and no delete method. Re-pricing a slot appends a new version
    that records which one it supersedes, and withdrawing a rate appends a tombstone (a row with a
    NULL price). Both are what make a past cost explainable: the rate that priced a span in March is
    still in the table in June. Migration 0035 backs the same rule with a trigger, so a stray UPDATE
    from a psql session is refused too.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def load_all(self) -> list[OverrideRecord]:
        """Every record for every tenant, in ledger order.

        Ordered by version within a slot because :meth:`OverrideLedger.active` resolves a slot
        last-write-wins: out of order, a tombstone could be overtaken by the rate it withdrew.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM price_catalog_overrides "
                "ORDER BY tenant_id, provider, model, price_type, version"
            )
            return [_row_to_record(row) for row in cur.fetchall()]

    def load_tenant_spellings(self) -> dict[str, list[str]]:
        """Every other spelling each tenant UUID answers to: its name and its Clerk org id.

        WHY the catalog needs this. The ledger keys on ``tenants.id``, as every control-plane table
        does, but ``/v1/batches`` stores and enriches under the spelling the CALLER posted and does
        NOT fold a name onto the UUID (see CLAUDE.md, tenant identity). The override pool is a plain
        dict keyed by that string, so an override loaded only under the UUID would price nothing at
        all for a tenant whose SDK posts ``local-dev``, and it would fail SILENTLY: the span would
        simply come back at list price. Registering the same entry under every spelling of the same
        tenant is what makes the override apply however the batch is addressed.

        An AMBIGUOUS spelling is dropped rather than picked between. ``tenants.name`` carries no
        unique constraint (``clerk_org_id`` does), so two tenants can legitimately both be called
        ``local-dev``, and a batch posted under that name does not say which one it belongs to.
        Registering one tenant's contract rate under a name another tenant also posts would price
        that second tenant's spans from a contract they never signed, which is the cross-tenant
        version of the exact failure this ticket exists to prevent. An ambiguous name therefore gets
        no override and falls back to the public catalog, and the UUID spelling keeps working.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, clerk_org_id FROM tenants")
            return unambiguous_spellings(
                [(str(r[0]), [str(v) for v in (r[1], r[2]) if v]) for r in cur.fetchall()]
            )

    def history(self, tenant_id: str) -> list[OverrideRecord]:
        """One tenant's full audit trail, oldest first, tombstones included."""
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                f"SELECT {_COLUMNS} FROM price_catalog_overrides WHERE tenant_id = %s "
                "ORDER BY provider, model, price_type, version",
                (str(resolved),),
            )
            return [_row_to_record(row) for row in cur.fetchall()]

    def append(
        self,
        tenant_id: str,
        *,
        provider: str,
        model: str,
        price_type: PriceType,
        price_per_unit: Decimal | None,
        actor: str,
        reason: str,
        unit: Unit = Unit.PER_MILLION_TOKENS,
        valid_from: date | None = None,
        valid_to: date | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> OverrideRecord:
        """Append one ledger entry. ``price_per_unit=None`` is a revocation tombstone.

        The version is assigned by the INSERT (``MAX(version) + 1`` over the slot) rather than by
        the in-process :class:`OverrideLedger`, because production runs more than one replica and
        two in-memory counters would hand out the same version for two different rates. The unique
        index on ``(tenant_id, provider, model, price_type, version)`` is what actually decides the
        race; the loser gets a 409 and can re-read and retry, which is the honest answer since its
        entry was written against a rate that is no longer current.
        """
        if valid_to is not None and valid_from is not None and valid_to < valid_from:
            raise PriceOverrideError("valid_to must be on or after valid_from")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = str(resolve_tenant_uuid(cur, tenant_id))
            start = valid_from or datetime.now().date()
            try:
                cur.execute(
                    f"""
                    INSERT INTO price_catalog_overrides
                        (tenant_id, provider, model, price_type, unit, price_per_unit, currency,
                         valid_from, valid_to, version, supersedes, actor, reason)
                    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           COALESCE(MAX(version), 0) + 1, MAX(version), %s, %s
                      FROM price_catalog_overrides
                     WHERE tenant_id = %s AND provider = %s AND model = %s AND price_type = %s
                    RETURNING {_COLUMNS}
                    """,
                    (
                        resolved,
                        provider,
                        model,
                        price_type.value,
                        unit.value,
                        price_per_unit,
                        currency,
                        start,
                        valid_to,
                        actor,
                        reason,
                        resolved,
                        provider,
                        model,
                        price_type.value,
                    ),
                )
            except pg_errors.UniqueViolation as exc:
                conn.rollback()
                raise PriceOverrideConflict(
                    "another change to this price landed first; re-read the ledger and retry"
                ) from exc
            row = cur.fetchone()
            assert row is not None  # RETURNING on an INSERT that always writes exactly one row
            conn.commit()
            return _row_to_record(row)


# --- refresh -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverrideLoadStatus:
    """The outcome of the most recent load attempt, as a health signal rather than a log line."""

    enabled: bool
    healthy: bool
    record_count: int
    active_count: int
    loaded_at: datetime | None
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "healthy": self.healthy,
            "record_count": self.record_count,
            "active_count": self.active_count,
            # null, not a zero timestamp: "never loaded" is a different state from "loaded at the
            # epoch", and only one of them is true here.
            "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
            "error": self.error,
        }


class PriceOverrideRefresher:
    """Materializes the durable ledger onto one live :class:`~tally.pricing.PriceCatalog`.

    It mutates the catalog IN PLACE (clear the override pool, re-apply the active records) rather
    than building a new catalog and swapping ``app.state.catalog``. A swap would leave any request
    that had already captured the old object enriching against a stale pool, and several call sites
    do exactly that capture.
    """

    def __init__(
        self,
        catalog: PriceCatalog,
        store: PriceOverrideStore | None,
        *,
        enabled: bool,
        ttl_s: float,
        monotonic: object = time.monotonic,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._enabled = enabled and store is not None
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        self._last_attempt_at: float | None = None
        self._status = OverrideLoadStatus(
            enabled=self._enabled,
            # Disabled is healthy: the deployment has said it holds no overrides, so the public
            # catalog is the whole truth and there is nothing that could be missing.
            healthy=not self._enabled,
            record_count=0,
            active_count=0,
            loaded_at=None,
            error=None if self._enabled else "price overrides disabled",
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> OverrideLoadStatus:
        return self._status

    def refresh(self) -> OverrideLoadStatus:
        """Reload the ledger and re-materialize it. Blocking; safe to call from a worker thread.

        On ANY failure the override pool is dropped and the catalog is marked unavailable, so
        tenant-scoped pricing goes to NULL / 'unpriced' instead of quietly reverting to list price.
        A stale pool is not kept either: a rate that was withdrawn an hour ago is just as wrong as a
        list price, and keeping it would make the failure invisible.
        """
        if not self._enabled or self._store is None:
            return self._status
        with self._lock:
            self._last_attempt_at = self._monotonic()  # type: ignore[operator]
            try:
                records = self._store.load_all()
                spellings = self._store.load_tenant_spellings()
            except Exception as exc:  # noqa: BLE001 - every failure mode gets the same honest answer
                self._catalog.clear_overrides()
                self._catalog.mark_overrides_unavailable(f"price override load failed: {exc}")
                self._status = OverrideLoadStatus(
                    enabled=True,
                    healthy=False,
                    record_count=0,
                    active_count=0,
                    loaded_at=self._status.loaded_at,
                    error=str(exc),
                )
                # ERROR, not warning: from here every LLM span prices as unknown, which is correct
                # but is not a state anyone should have to discover from a dashboard full of blanks.
                logger.error(
                    "price overrides: load FAILED (%s); tenant-scoped costs will be reported as "
                    "unpriced until a refresh succeeds",
                    exc,
                )
                return self._status

            ledger = OverrideLedger.from_records(records)
            active = ledger.active()
            self._catalog.clear_overrides()
            ledger.apply_to_catalog(self._catalog)
            # ...and again under every other spelling of the same tenant, because the ingest path
            # enriches under the id the caller posted rather than the resolved UUID. See
            # PriceOverrideStore.load_tenant_spellings for why leaving this out fails silently.
            for record in active:
                entry = record.to_price_entry()
                if entry is None:
                    continue
                for alias in spellings.get(record.tenant_id, ()):
                    self._catalog.add_override(alias, entry)
            self._catalog.mark_overrides_loaded()
            self._status = OverrideLoadStatus(
                enabled=True,
                healthy=True,
                record_count=len(records),
                active_count=len(active),
                loaded_at=datetime.now().astimezone(),
                error=None,
            )
            logger.info(
                "price overrides: loaded %d ledger entries, %d active",
                len(records),
                len(active),
            )
            return self._status

    def _is_stale(self) -> bool:
        if not self._enabled:
            return False
        if self._last_attempt_at is None:
            return True
        return (self._monotonic() - self._last_attempt_at) >= self._ttl_s  # type: ignore[operator]

    async def ensure_fresh(self) -> None:
        """TTL-bounded reload from the ingest path, off the event loop.

        This is what carries a price written on one replica to the others. The check is a clock
        comparison on the hot path; only an expired window pays for a query, and that query runs in
        a worker thread so a slow Postgres delays the batch that triggered it rather than the loop.
        """
        if not self._is_stale():
            return
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            # Re-check under the lock: while this coroutine waited, another one may have refreshed.
            if not self._is_stale():
                return
            await asyncio.to_thread(self.refresh)
