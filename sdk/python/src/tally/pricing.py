# SPDX-License-Identifier: Apache-2.0
"""Price catalog: versioned, multi-provider rate table + cost computation.

Implements CTO-52.

All ``EstimatedCost`` derives from this table. It is *versioned* and *time-windowed* so historical
cost can be recomputed if a rate is corrected, and so a price change doesn't retroactively rewrite
past cost. Per-tenant overrides take precedence over the public catalog (enterprise contracts).

Rates are :class:`~decimal.Decimal` (never float; this is money). Cost is returned as integer
micro-USD via :func:`tally.schema.usd_to_micro`.

The seed data here is illustrative and meant to be replaced by the daily scraper (CTO-53); treat
the *shape* as authoritative, the *numbers* as placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from tally.schema import DEFAULT_CURRENCY, usd_to_micro


class PriceType(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    CACHED_INPUT = "cached_input"
    TOOL_CALL = "tool_call"
    VECTOR_CALL = "vector_call"
    EMBEDDING = "embedding"


class Unit(str, Enum):
    PER_MILLION_TOKENS = "per_million_tokens"
    PER_CALL = "per_call"
    PER_GB = "per_gb"


#: Which units each price tier can actually be priced in (CTO-416).
#:
#: This is not taxonomy for its own sake: :func:`_line` only knows how to turn PER_MILLION_TOKENS
#: and PER_CALL into money, and every other pairing used to fall through to ``Decimal(0)``. A
#: per-GB rate on an ``input`` tier therefore produced a cost of exactly zero WITH a catalog
#: version attached, so the span read as confidently priced at nothing rather than as unpriced.
#: Callers that accept a rate from a human (the gateway's price-override control plane) validate
#: against this map at the boundary, and :meth:`PriceCatalog._best` skips anything that slipped
#: past so the answer is a miss rather than a fabricated zero.
PRICEABLE_UNITS: dict[PriceType, frozenset[Unit]] = {
    PriceType.INPUT: frozenset({Unit.PER_MILLION_TOKENS}),
    PriceType.OUTPUT: frozenset({Unit.PER_MILLION_TOKENS}),
    PriceType.CACHED_INPUT: frozenset({Unit.PER_MILLION_TOKENS}),
    PriceType.EMBEDDING: frozenset({Unit.PER_MILLION_TOKENS}),
    PriceType.TOOL_CALL: frozenset({Unit.PER_CALL}),
    PriceType.VECTOR_CALL: frozenset({Unit.PER_CALL}),
}


def unit_can_price(price_type: PriceType, unit: Unit) -> bool:
    """True when a rate in ``unit`` can actually be applied to ``price_type``.

    Unknown tiers answer False rather than True: a tier nobody has taught the cost math about must
    not price anything until somebody does.
    """
    return unit in PRICEABLE_UNITS.get(price_type, frozenset())


def priceable_units(price_type: PriceType) -> list[str]:
    """The unit spellings a caller may offer for ``price_type``, for a control-plane response."""
    return sorted(u.value for u in PRICEABLE_UNITS.get(price_type, frozenset()))


@dataclass(frozen=True, slots=True)
class PriceEntry:
    version: str
    valid_from: date
    provider: str
    model: str
    price_type: PriceType
    unit: Unit
    price_per_unit: Decimal
    currency: str = DEFAULT_CURRENCY
    valid_to: date | None = None

    def is_valid_at(self, at: date) -> bool:
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)


class PriceCatalogMiss(Exception):
    """No applicable price entry was found for the lookup."""


class PriceCatalog:
    """In-memory price catalog with time-windowed lookup and per-tenant overrides.

    PRECEDENCE, stated once and authoritatively (CTO-416). A tenant's own override beats the public
    catalog for every lookup that carries a ``tenant_id``: the contract the customer signed is what
    they are billed, and a public list price is only the fallback for a slot they have not
    negotiated. Within either pool the most recent applicable ``valid_from`` wins, and the exact
    model id is tried before its family (see :meth:`lookup`). ``docs/price-overrides.md`` is the
    customer-facing statement of the same rule.

    OVERRIDES UNAVAILABLE. The overrides in this object are a materialized copy of a durable ledger
    (``price_catalog_overrides``, :class:`tally.overrides.OverrideLedger`). When whoever loads that
    ledger cannot read it, they call :meth:`mark_overrides_unavailable` and every tenant-scoped
    lookup then answers ``None`` instead of a public rate. That is deliberate and it is the honesty
    invariant, not a bug: we cannot tell which slots a tenant has negotiated while the ledger is
    unreadable, so pricing one from the public table would report spend the customer did not incur.
    An honest blank is recoverable; a confident wrong number is not.
    """

    def __init__(self, entries: list[PriceEntry] | None = None) -> None:
        self._entries: list[PriceEntry] = list(entries or [])
        # CTO-416: the override pool AND whether it can be trusted, held as ONE value.
        #
        # They are one attribute rather than two because a reader must never see a mix of them. The
        # ingest path enriches on the event loop while a refresh runs on a worker thread, and
        # neither takes a lock, so with two attributes there is always an instant between the two
        # writes: an empty pool that still advertises itself as loaded, in which a contract tenant's
        # lookup falls through to the PUBLIC list price. One tuple, one assignment, and under the
        # GIL a reader gets the old pair or the new pair and never half of each.
        self._override_state: tuple[dict[str, list[PriceEntry]], str | None] = ({}, None)

    @property
    def _overrides(self) -> dict[str, list[PriceEntry]]:
        return self._override_state[0]

    def add(self, entry: PriceEntry) -> None:
        self._entries.append(entry)

    def add_override(self, tenant_id: str, entry: PriceEntry) -> None:
        """Append ONE override entry to the live pool.

        For building a pool a step at a time (a test, or ``OverrideLedger.apply_to_catalog`` onto a
        catalog nothing is reading yet). A reloader of a LIVE catalog uses :meth:`replace_overrides`
        instead: see the constructor for why an incremental rebuild is visible to readers.
        """
        pool, unavailable = self._override_state
        pool.setdefault(tenant_id, []).append(entry)
        if unavailable is not None:
            # Adding an entry to a pool flagged unreadable would leave an override nothing can use.
            self._override_state = (pool, None)

    def clear_overrides(self) -> None:
        """Drop every materialized override entry (CTO-416).

        This changes the pool in place rather than swapping the whole catalog object, because
        callers hold a reference to it (``app.state.catalog``) and a swap would leave a request
        enriching against the catalog it captured before the change.

        A reloader must NOT use this to rebuild: clearing and re-adding leaves a window in which the
        pool is empty but nothing says so, and a lookup landing in that window prices a contract
        tenant at public list. Use :meth:`replace_overrides`, which installs in one assignment.
        """
        self._override_state = ({}, self._override_state[1])

    def replace_overrides(self, pool: dict[str, list[PriceEntry]]) -> None:
        """Install a freshly built override pool and mark it loaded, in ONE step (CTO-416).

        WHY this exists rather than clear-then-add. ``refresh`` runs on a worker thread while the
        ingest path keeps enriching, and readers take no lock. Rebuilding in place meant every
        reader during the rebuild saw a partial (often empty) pool with the fail-closed flag already
        cleared, so a tenant on a contract was priced at LIST for the duration, on every refresh
        window rather than only on failure.

        The pool is swapped, never the catalog object, so a caller holding a reference to this
        catalog (``app.state.catalog``) still observes the change.
        """
        self._override_state = (
            {tenant: list(entries) for tenant, entries in pool.items()},
            None,
        )

    @property
    def overrides_unavailable(self) -> str | None:
        """Why the override pool is untrustworthy right now, or ``None`` when it is loaded."""
        return self._override_state[1]

    def mark_overrides_unavailable(self, reason: str) -> None:
        """Fail CLOSED: tenant-scoped lookups answer ``None`` until overrides load again (CTO-416).

        Drops the pool and raises the flag in the same assignment. Done as two statements in either
        order, one of them is briefly visible without the other, and the order that leaves an empty
        pool looking trustworthy is exactly the list-price fallback this flag exists to prevent.
        """
        self._override_state = ({}, reason)

    def mark_overrides_loaded(self) -> None:
        """Clear the fail-closed flag after a successful ledger load, keeping the current pool."""
        self._override_state = (self._override_state[0], None)

    def _best(
        self, pool: list[PriceEntry], provider: str, model: str, price_type: PriceType, at: date
    ) -> PriceEntry | None:
        candidates = [
            e
            for e in pool
            if e.provider == provider
            and e.model == model
            and e.price_type == price_type
            and e.is_valid_at(at)
            # CTO-416: an entry whose unit cannot price this tier (a per_gb rate on an input tier)
            # is a MISS, not a hit worth zero. _line has no arithmetic for it, so honouring the
            # entry produced a confident cost of 0 carrying a real catalog version: a fabricated
            # zero, which is the one thing the Nullable cost columns exist to prevent. Skipping it
            # leaves the span unpriced, which is the honest answer for a rate we cannot apply.
            and unit_can_price(e.price_type, e.unit)
        ]
        if not candidates:
            return None
        # most recent applicable valid_from wins
        return max(candidates, key=lambda e: e.valid_from)

    def lookup(
        self,
        provider: str,
        model: str,
        price_type: PriceType,
        *,
        at: date | None = None,
        tenant_id: str | None = None,
    ) -> PriceEntry | None:
        at = at or date.today()
        # CTO-416: read the pool and its trustworthiness ONCE, as the pair they are stored as, so a
        # refresh landing mid-lookup cannot have this call price from an empty pool that the flag
        # said was loaded. See PriceCatalog.__init__.
        overrides, unavailable = self._override_state
        # The ledger behind the pool could not be read, so we do not know whether this tenant has a
        # negotiated rate for this slot. Answer "unknown" rather than the public list price: a
        # contract rate is usually BELOW list, so falling back would over-report spend the customer
        # never incurred, and it would look exactly like a real number. The caller
        # (tally.enrichment) turns a None into a NULL cost with CostSource 'unpriced'.
        if unavailable is not None and tenant_id:
            return None
        # CTO-368: providers report the snapshot they served (claude-haiku-4-5-20251001,
        # gpt-4o-mini-2024-07-18) while the catalog lists families, so an exact-only match left
        # real calls unpriced. The exact id is tried first, so a snapshot priced unlike its family
        # can still be listed on its own. A tenant's contract is checked before the public table at
        # both steps: a contract on the family is what the tenant pays for every snapshot of it.
        models = [model]
        family = _family_model(model)
        if family is not None:
            models.append(family)
        pools = [self._entries]
        if tenant_id and tenant_id in overrides:
            pools.insert(0, overrides[tenant_id])
        for pool in pools:
            for candidate in models:
                hit = self._best(pool, provider, candidate, price_type, at)
                if hit is not None:
                    return hit
        return None


def _family_model(model: str) -> str | None:
    """Strip a trailing snapshot date (``-YYYYMMDD`` or ``-YYYY-MM-DD``) from a model id.

    Returns ``None`` unless the suffix is a real calendar date with a model name left in front of
    it. Anything else (``gpt-4-0613``, ``-20251399``, ``-v2``) stays a miss rather than borrowing a
    family price it may not have.
    """
    if len(model) > 9 and model[-9] == "-" and model[-8:].isascii() and model[-8:].isdigit():
        digits = model[-8:]
        iso, family = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}", model[:-9]
    elif len(model) > 11 and model[-11] == "-" and model[-10:].isascii():
        iso, family = model[-10:], model[:-11]
        if not (iso[4] == "-" and iso[7] == "-" and iso.replace("-", "").isdigit()):
            return None
    else:
        return None
    try:
        date.fromisoformat(iso)
    except ValueError:
        return None
    return family


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


def compute_cost_micro_usd(
    catalog: PriceCatalog,
    provider: str,
    model: str,
    usage: Usage,
    *,
    at: date | None = None,
    tenant_id: str | None = None,
    strict: bool = False,
) -> tuple[int, str]:
    """Compute estimated cost in micro-USD for a chat/completion call.

    Cached input tokens are billed at the cached rate when available, and the remaining
    (input - cached) at the standard input rate.

    Returns ``(micro_usd, catalog_version)``.

    Raises :class:`PriceCatalogMiss` when ``strict`` and a required rate is missing; otherwise
    missing components contribute 0 (and an empty version string signals a partial price).
    """
    at = at or date.today()
    total_usd = Decimal(0)
    version = ""

    def rate(pt: PriceType) -> PriceEntry | None:
        return catalog.lookup(provider, model, pt, at=at, tenant_id=tenant_id)

    input_entry = rate(PriceType.INPUT)
    output_entry = rate(PriceType.OUTPUT)
    cached_entry = rate(PriceType.CACHED_INPUT)

    if strict and (input_entry is None or output_entry is None):
        raise PriceCatalogMiss(f"missing input/output price for {provider}/{model} at {at}")

    cached_tokens = min(usage.cached_input_tokens, usage.input_tokens)
    uncached_input = usage.input_tokens - cached_tokens

    if input_entry is not None:
        total_usd += _line(input_entry, uncached_input)
        version = input_entry.version
    if cached_entry is not None and cached_tokens:
        total_usd += _line(cached_entry, cached_tokens)
    elif input_entry is not None and cached_tokens:
        # no cached rate → fall back to standard input rate for cached tokens
        total_usd += _line(input_entry, cached_tokens)
    if output_entry is not None:
        total_usd += _line(output_entry, usage.output_tokens)
        version = output_entry.version or version

    return usd_to_micro(total_usd), version


def compute_embedding_cost_micro_usd(
    catalog: PriceCatalog,
    provider: str,
    model: str,
    input_tokens: int,
    *,
    at: date | None = None,
    tenant_id: str | None = None,
) -> tuple[int, str]:
    """Compute embedding cost in micro-USD.

    Resolves the ``PriceType.EMBEDDING`` tier, NOT ``INPUT``. The seed catalog prices embedding
    models under ``EMBEDDING`` (see ``text-embedding-3-*``), so ``compute_cost_micro_usd`` (which
    only looks at INPUT/OUTPUT) would return 0 for them. Returns ``(micro_usd, catalog_version)``;
    ``(0, "")`` when no embedding rate is seeded for ``(provider, model)`` at ``at``.
    """
    at = at or date.today()
    entry = catalog.lookup(provider, model, PriceType.EMBEDDING, at=at, tenant_id=tenant_id)
    if entry is None:
        return 0, ""
    return usd_to_micro(_line(entry, input_tokens)), entry.version


def compute_call_cost_micro_usd(
    catalog: PriceCatalog,
    provider: str,
    name: str,
    price_type: PriceType,
    *,
    at: date | None = None,
    tenant_id: str | None = None,
) -> tuple[int, str]:
    """Compute the flat per-call cost in micro-USD for a non-LLM call.

    Used for tool calls (``PriceType.TOOL_CALL``) and vector-DB calls
    (``PriceType.VECTOR_CALL``), both of which price per *call* rather than per token. ``name`` is
    the catalog ``model`` slot, e.g. the tool name (``"search"``) or the vector operation
    (``"query"``). The matching seed entry must use ``Unit.PER_CALL``.

    Returns ``(micro_usd, catalog_version)``; ``(0, "")`` when no entry is seeded for
    ``(provider, name, price_type)`` at ``at``.
    """
    at = at or date.today()
    entry = catalog.lookup(provider, name, price_type, at=at, tenant_id=tenant_id)
    if entry is None:
        return 0, ""
    return usd_to_micro(_line(entry, 1)), entry.version


def _line(entry: PriceEntry, tokens: int) -> Decimal:
    if entry.unit is Unit.PER_MILLION_TOKENS:
        return entry.price_per_unit * Decimal(tokens) / Decimal(1_000_000)
    if entry.unit is Unit.PER_CALL:
        return entry.price_per_unit
    # CTO-416: raise rather than return Decimal(0). The zero was indistinguishable from a real free
    # call and travelled with the entry's catalog version, so a whole tier could read as costing
    # nothing. Nothing should reach here any more: PriceCatalog._best drops an entry whose unit
    # cannot price its tier, and the override control plane refuses the pairing at the boundary.
    # This is the assertion that keeps the next unit added to the enum from reintroducing the zero.
    raise ValueError(
        f"no cost arithmetic for unit {entry.unit.value} on price type {entry.price_type.value}; "
        "see PRICEABLE_UNITS"
    )


# --- Seed data (illustrative; replaced by the scraper, CTO-53) ----------------------------------
#
# Hand-maintained until the pricing scraper lands; rates valid as of 2026-06-15.
# Expanded for CTO-106 to cover the OpenAI + Anthropic models the example demos
# actually call. Previously the gpt-5-mini pinning workaround in examples/* was
# needed because of catalog gaps (CTO-104/CTO-105); once the catalog knows the
# real models the workaround can come out. Live pricing pages should be the
# source of truth; any rate below tagged "[unverified at implementation time]"
# could not be reached at edit time and was filled in from training-data values.

_SEED_VERSION = "seed-2026-06-15"
# valid_from kept at 2026-05-01 (the prior catalog window) so any test or
# replay at the existing 2026-06-01 cutover keeps resolving; the 2026-06-15
# date in the header is the verification date for the *rates*, not the
# valid_from window.
_SEED_FROM = date(2026, 5, 1)


def _mtok(provider: str, model: str, pt: PriceType, usd_per_mtok: str) -> PriceEntry:
    return PriceEntry(
        version=_SEED_VERSION,
        valid_from=_SEED_FROM,
        provider=provider,
        model=model,
        price_type=pt,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(usd_per_mtok),
    )


def _per_call(provider: str, name: str, pt: PriceType, usd_per_call: str) -> PriceEntry:
    return PriceEntry(
        version=_SEED_VERSION,
        valid_from=_SEED_FROM,
        provider=provider,
        model=name,
        price_type=pt,
        unit=Unit.PER_CALL,
        price_per_unit=Decimal(usd_per_call),
    )


# Per-call tool + vector-DB rates (CTO-141). Promoted from the inline ``_TOOL_PRICING`` /
# ``_VECTOR_PRICING`` dicts that PR #111 / #116 carried in client.py as stopgaps. Prices in USD;
# kept consistent with the micro-USD values they replace (e.g. tavily search $0.01 == 10_000
# micro). The ``model`` slot holds the tool name (tools) or operation (vector). All rates
# [unverified at implementation time].
_TOOL_SEEDS: list[tuple[str, str, PriceType, str]] = [
    # Tools: $0.01 tavily == 10_000 micro, etc.
    ("tavily", "search", PriceType.TOOL_CALL, "0.01"),
    ("serpapi", "search", PriceType.TOOL_CALL, "0.015"),
    ("brave", "search", PriceType.TOOL_CALL, "0.005"),
    ("firecrawl", "scrape", PriceType.TOOL_CALL, "0.02"),
    ("exa", "search", PriceType.TOOL_CALL, "0.01"),
    ("you.com", "search", PriceType.TOOL_CALL, "0.01"),
    ("bing", "search", PriceType.TOOL_CALL, "0.007"),
    ("openai", "code_interpreter", PriceType.TOOL_CALL, "0.03"),
]
_VECTOR_SEEDS: list[tuple[str, str, PriceType, str]] = [
    # Vector DB: keep micro-USD parity with the old inline dict (pinecone query 400 micro).
    ("pinecone", "query", PriceType.VECTOR_CALL, "0.0004"),
    ("pinecone", "upsert", PriceType.VECTOR_CALL, "0.0002"),
    ("weaviate", "query", PriceType.VECTOR_CALL, "0.0003"),
    ("qdrant", "query", PriceType.VECTOR_CALL, "0.00025"),
    # --- Vertex AI Vector Search (formerly Matching Engine), CTO-151 -------------------------
    # GCP-native vector provider. Keyed off the operation name (query/upsert) like the other
    # vector DBs above, so it resolves through the same compute_call_cost_micro_usd path in
    # record_vector_call (provider="vertex"). Mirrors the CTO-142 span shape, no new wrapper.
    #
    # COST SPLIT (important, avoids double-counting):
    # Vertex Vector Search bills in two parts:
    #   1. Per-query / serving requests: the online-query request portion. Priced HERE, on the
    #      Vector cost layer, per call.
    #   2. Deployed-index node-hours: the compute cost of keeping the index endpoint warm
    #      (the dominant Vertex Vector Search spend). This is a COMPUTE cost, NOT a per-query
    #      cost, and is DEFERRED to the GCP Cloud Billing compute connector (CTO-150). It is
    #      deliberately NOT modeled here so the Vector layer and the future Compute layer do not
    #      both count node-hours. See CTO-150.
    # Both rates below are the per-query serving portion only. [unverified at implementation time]
    ("vertex", "query", PriceType.VECTOR_CALL, "0.0004"),
    ("vertex", "upsert", PriceType.VECTOR_CALL, "0.0002"),
]


def seed_catalog() -> PriceCatalog:
    """Multi-provider seed catalog (OpenAI + Anthropic + Google Gemini/Vertex + Amazon Bedrock).

    Expanded for CTO-106; previously the gpt-5-mini pinning workaround in
    examples/* was needed because of catalog gaps. The scraper (CTO-53) will
    eventually own this; until then these are hand-maintained rates.

    Rates are USD per million tokens unless noted. Except for the Fireworks AI
    entries (CTO-418), which are owner-supplied and confirmed on 2026-09-17,
    all rates below are
    [unverified at implementation time]: the live pricing pages were not
    reachable from the implementation environment, so values are taken from
    the assistant's training data and reflect publicly-listed prices as of
    early 2026. Update once the scraper lands.
    """
    cat = PriceCatalog()
    seeds: list[tuple[str, str, PriceType, str]] = [
        # --- OpenAI (https://openai.com/api/pricing/) -------------------------
        # Legacy gpt-5 family: kept for backward compat with existing tests.
        ("openai", "gpt-5-mini", PriceType.INPUT, "0.25"),
        ("openai", "gpt-5-mini", PriceType.CACHED_INPUT, "0.025"),
        ("openai", "gpt-5-mini", PriceType.OUTPUT, "2.00"),
        ("openai", "gpt-5", PriceType.INPUT, "2.50"),
        ("openai", "gpt-5", PriceType.CACHED_INPUT, "0.25"),
        ("openai", "gpt-5", PriceType.OUTPUT, "10.00"),
        # gpt-4o family. [unverified at implementation time]
        ("openai", "gpt-4o", PriceType.INPUT, "2.50"),
        ("openai", "gpt-4o", PriceType.CACHED_INPUT, "1.25"),
        ("openai", "gpt-4o", PriceType.OUTPUT, "10.00"),
        ("openai", "gpt-4o-mini", PriceType.INPUT, "0.15"),
        ("openai", "gpt-4o-mini", PriceType.CACHED_INPUT, "0.075"),
        ("openai", "gpt-4o-mini", PriceType.OUTPUT, "0.60"),
        # gpt-4-turbo: no cached-input tier listed. [unverified at implementation time]
        ("openai", "gpt-4-turbo", PriceType.INPUT, "10.00"),
        ("openai", "gpt-4-turbo", PriceType.OUTPUT, "30.00"),
        # Embeddings. [unverified at implementation time]
        ("openai", "text-embedding-3-small", PriceType.EMBEDDING, "0.02"),
        ("openai", "text-embedding-3-large", PriceType.EMBEDDING, "0.13"),
        # --- Anthropic (https://anthropic.com/pricing) ------------------------
        # Anthropic prices cache_creation and cache_read separately; we map
        # CACHED_INPUT to the cheaper cache-read tier (the steady-state read
        # price, which dominates for repeated prompts). Cache-creation writes
        # are a one-shot premium not modeled in the current PriceType enum.
        # All rates [unverified at implementation time].
        ("anthropic", "claude-sonnet-4-5", PriceType.INPUT, "3.00"),
        ("anthropic", "claude-sonnet-4-5", PriceType.CACHED_INPUT, "0.30"),
        ("anthropic", "claude-sonnet-4-5", PriceType.OUTPUT, "15.00"),
        ("anthropic", "claude-haiku-4-5", PriceType.INPUT, "1.00"),
        ("anthropic", "claude-haiku-4-5", PriceType.CACHED_INPUT, "0.10"),
        ("anthropic", "claude-haiku-4-5", PriceType.OUTPUT, "5.00"),
        ("anthropic", "claude-opus-4-8", PriceType.INPUT, "15.00"),
        ("anthropic", "claude-opus-4-8", PriceType.CACHED_INPUT, "1.50"),
        ("anthropic", "claude-opus-4-8", PriceType.OUTPUT, "75.00"),
        # --- Google Gemini / Vertex AI (https://ai.google.dev/pricing) --------
        # CTO-149. Provider string "google" matches gen_ai.system and the Compare
        # mock's provider: "google". Gemini exposes both the Gemini API and Vertex
        # AI; per-token rates are the same across both surfaces (Vertex bills the
        # identical published rate), so one catalog entry covers both. Gemini's
        # "context cache" read tier maps to CACHED_INPUT (the steady-state read
        # price for repeated prompts); the one-shot cache-storage fee is not
        # modeled by the current PriceType enum. Text tokens only for v1
        # (multimodal token accounting is out of scope). Usage field mapping:
        # promptTokenCount -> input, candidatesTokenCount -> output,
        # cachedContentTokenCount -> cached_input. All rates
        # [unverified at implementation time]: taken from training-data values
        # for the published per-MTok prices; update once the scraper (CTO-53) lands.
        ("google", "gemini-2.5-flash", PriceType.INPUT, "0.30"),
        ("google", "gemini-2.5-flash", PriceType.CACHED_INPUT, "0.075"),
        ("google", "gemini-2.5-flash", PriceType.OUTPUT, "2.50"),
        ("google", "gemini-2.5-pro", PriceType.INPUT, "1.25"),
        ("google", "gemini-2.5-pro", PriceType.CACHED_INPUT, "0.31"),
        ("google", "gemini-2.5-pro", PriceType.OUTPUT, "10.00"),
        # gemini-3-flash: the id the /compare mock lists. Priced in line with the
        # 2.5-flash tier pending a verified published rate. [unverified at implementation time]
        ("google", "gemini-3-flash", PriceType.INPUT, "0.30"),
        ("google", "gemini-3-flash", PriceType.CACHED_INPUT, "0.075"),
        ("google", "gemini-3-flash", PriceType.OUTPUT, "2.50"),
        # --- Amazon Bedrock (https://aws.amazon.com/bedrock/pricing/) ---------
        # CTO-157. Bedrock is a *managed* re-seller of third-party + Amazon-first
        # models, so it gets its own provider dimension ("bedrock") rather than
        # sharing the vendor-direct keys. The model slot holds Bedrock's native
        # modelId minus the "-vN:0" version tag (e.g. "anthropic.claude-sonnet-4-5"),
        # which is exactly what a Bedrock caller passes as gen_ai.request_model and
        # what list_foundation_models advertises. This deliberately does NOT collide
        # with the vendor-direct entries above (provider="anthropic",
        # model="claude-sonnet-4-5"): same model, different surface, different price.
        #
        # Bedrock re-prices some models ABOVE the vendor-direct rate (the managed
        # infra premium): e.g. Bedrock's Claude Sonnet output is listed higher than
        # Anthropic-direct. These capture Bedrock's OWN published on-demand rates,
        # not the vendor-direct numbers. Bedrock's prompt-caching read tier maps to
        # CACHED_INPUT (steady-state read price); the cache-write premium is not
        # modeled by the current PriceType enum. Usage field mapping mirrors the
        # Bedrock Converse API: usage.inputTokens -> input,
        # usage.outputTokens -> output, usage.cacheReadInputTokens -> cached_input.
        # All rates [unverified at implementation time]: the live Bedrock pricing
        # page was not reachable from the implementation environment, so values are
        # taken from training-data values for the published per-MTok on-demand
        # prices; update once the scraper (CTO-53) lands.
        #
        # Anthropic on Bedrock: priced above Anthropic-direct (managed premium).
        ("bedrock", "anthropic.claude-sonnet-4-5", PriceType.INPUT, "3.30"),
        ("bedrock", "anthropic.claude-sonnet-4-5", PriceType.CACHED_INPUT, "0.33"),
        ("bedrock", "anthropic.claude-sonnet-4-5", PriceType.OUTPUT, "16.50"),
        ("bedrock", "anthropic.claude-haiku-4-5", PriceType.INPUT, "1.10"),
        ("bedrock", "anthropic.claude-haiku-4-5", PriceType.CACHED_INPUT, "0.11"),
        ("bedrock", "anthropic.claude-haiku-4-5", PriceType.OUTPUT, "5.50"),
        # Meta Llama on Bedrock: no cached tier published on-demand.
        ("bedrock", "meta.llama3-3-70b-instruct", PriceType.INPUT, "0.72"),
        ("bedrock", "meta.llama3-3-70b-instruct", PriceType.OUTPUT, "0.72"),
        # Amazon Nova (first-party flagship-ish + micro tiers).
        ("bedrock", "amazon.nova-pro", PriceType.INPUT, "0.80"),
        ("bedrock", "amazon.nova-pro", PriceType.CACHED_INPUT, "0.20"),
        ("bedrock", "amazon.nova-pro", PriceType.OUTPUT, "3.20"),
        ("bedrock", "amazon.nova-micro", PriceType.INPUT, "0.035"),
        ("bedrock", "amazon.nova-micro", PriceType.CACHED_INPUT, "0.00875"),
        ("bedrock", "amazon.nova-micro", PriceType.OUTPUT, "0.14"),
        # Amazon Titan (legacy first-party text): no cached tier.
        ("bedrock", "amazon.titan-text-express", PriceType.INPUT, "0.20"),
        ("bedrock", "amazon.titan-text-express", PriceType.OUTPUT, "0.60"),
        # --- Fireworks AI (https://fireworks.ai/pricing) ----------------------
        # CTO-418. The provider string is "fireworks-ai", NOT "fireworks": that is the spelling
        # the pilot's spans carry in gen_ai.system, confirmed against real production telemetry.
        # An entry keyed "fireworks" would match no traffic at all, so do not "tidy" it.
        # The model slot holds Fireworks' full account-scoped id exactly as it arrives in
        # gen_ai.request.model (accounts/fireworks/models/<name>); it is not shortened, because
        # the lookup is an exact match on what the caller sent.
        #
        # Rates below were supplied and confirmed by the account owner on 2026-09-17 (unlike the
        # surrounding entries, which are training-data values). No cached-input rate was supplied,
        # so none is listed: with no CACHED_INPUT tier, compute_cost_micro_usd bills cached tokens
        # at the full input rate, which OVERSTATES cost for cache-heavy traffic. Inventing a
        # discount would be worse (a fabricated number); source the real cached rates to fix it.
        ("fireworks-ai", "accounts/fireworks/models/kimi-k3", PriceType.INPUT, "3.00"),
        ("fireworks-ai", "accounts/fireworks/models/kimi-k3", PriceType.OUTPUT, "15.00"),
        ("fireworks-ai", "accounts/fireworks/models/qwen3p8-2p4t-a95b", PriceType.INPUT, "2.00"),
        ("fireworks-ai", "accounts/fireworks/models/qwen3p8-2p4t-a95b", PriceType.OUTPUT, "6.00"),
        # DELIBERATELY UNPRICED (CTO-418): accounts/fireworks/models/qwen3p7-plus. Fireworks does
        # not list it individually and its size band cannot be determined from the public docs, so
        # no rate is honest to assert; the pilot's single call reported zero tokens anyway. It
        # stays a catalog miss and renders blank rather than a guessed number. This omission is
        # intentional, not an oversight: add an entry only once a rate is confirmed.
    ]
    for provider, model, pt, rate in seeds:
        cat.add(_mtok(provider, model, pt, rate))
    # Per-call tool + vector entries (CTO-141), promoted from the inline client.py dicts.
    for provider, name, pt, rate in (*_TOOL_SEEDS, *_VECTOR_SEEDS):
        cat.add(_per_call(provider, name, pt, rate))
    return cat
