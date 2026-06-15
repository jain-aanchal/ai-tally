# SPDX-License-Identifier: Apache-2.0
"""Price catalog — versioned, multi-provider rate table + cost computation.

Implements CTO-52.

All ``EstimatedCost`` derives from this table. It is *versioned* and *time-windowed* so historical
cost can be recomputed if a rate is corrected, and so a price change doesn't retroactively rewrite
past cost. Per-tenant overrides take precedence over the public catalog (enterprise contracts).

Rates are :class:`~decimal.Decimal` (never float — this is money). Cost is returned as integer
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
    EMBEDDING = "embedding"


class Unit(str, Enum):
    PER_MILLION_TOKENS = "per_million_tokens"
    PER_CALL = "per_call"
    PER_GB = "per_gb"


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
    """In-memory price catalog with time-windowed lookup and per-tenant overrides."""

    def __init__(self, entries: list[PriceEntry] | None = None) -> None:
        self._entries: list[PriceEntry] = list(entries or [])
        # per-tenant override entries, keyed by tenant id
        self._overrides: dict[str, list[PriceEntry]] = {}

    def add(self, entry: PriceEntry) -> None:
        self._entries.append(entry)

    def add_override(self, tenant_id: str, entry: PriceEntry) -> None:
        self._overrides.setdefault(tenant_id, []).append(entry)

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
        if tenant_id and tenant_id in self._overrides:
            hit = self._best(self._overrides[tenant_id], provider, model, price_type, at)
            if hit is not None:
                return hit
        return self._best(self._entries, provider, model, price_type, at)


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


def _line(entry: PriceEntry, tokens: int) -> Decimal:
    if entry.unit is Unit.PER_MILLION_TOKENS:
        return entry.price_per_unit * Decimal(tokens) / Decimal(1_000_000)
    if entry.unit is Unit.PER_CALL:
        return entry.price_per_unit
    return Decimal(0)


# --- Seed data (illustrative; replaced by the scraper, CTO-53) ----------------------------------
#
# Hand-maintained until the pricing scraper lands; rates valid as of 2026-06-15.
# Expanded for CTO-106 to cover the OpenAI + Anthropic models the example demos
# actually call. Previously the gpt-5-mini pinning workaround in examples/* was
# needed because of catalog gaps (CTO-104/CTO-105) — once the catalog knows the
# real models the workaround can come out. Live pricing pages should be the
# source of truth; any rate below tagged "[unverified at implementation time]"
# could not be reached at edit time and was filled in from training-data values.

_SEED_VERSION = "seed-2026-06-15"
# valid_from kept at 2026-05-01 (the prior catalog window) so any test or
# replay at the existing 2026-06-01 cutover keeps resolving — the 2026-06-15
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


def seed_catalog() -> PriceCatalog:
    """Multi-provider seed catalog (OpenAI + Anthropic).

    Expanded for CTO-106; previously the gpt-5-mini pinning workaround in
    examples/* was needed because of catalog gaps. The scraper (CTO-53) will
    eventually own this; until then these are hand-maintained rates.

    Rates are USD per million tokens unless noted. All rates below are
    [unverified at implementation time] — the live pricing pages were not
    reachable from the implementation environment, so values are taken from
    the assistant's training data and reflect publicly-listed prices as of
    early 2026. Update once the scraper lands.
    """
    cat = PriceCatalog()
    seeds: list[tuple[str, str, PriceType, str]] = [
        # --- OpenAI (https://openai.com/api/pricing/) -------------------------
        # Legacy gpt-5 family — kept for backward compat with existing tests.
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
        # gpt-4-turbo — no cached-input tier listed. [unverified at implementation time]
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
    ]
    for provider, model, pt, rate in seeds:
        cat.add(_mtok(provider, model, pt, rate))
    return cat
