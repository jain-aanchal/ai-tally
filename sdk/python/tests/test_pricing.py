# SPDX-License-Identifier: Apache-2.0
from datetime import date
from decimal import Decimal

import pytest

from tally.pricing import (
    PriceCatalog,
    PriceCatalogMiss,
    PriceEntry,
    PriceType,
    Unit,
    Usage,
    compute_cost_micro_usd,
    seed_catalog,
)


def _entry(model, pt, rate, version="v1", valid_from=date(2026, 5, 1), valid_to=None):
    return PriceEntry(
        version=version,
        valid_from=valid_from,
        provider="openai",
        model=model,
        price_type=pt,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(rate),
        valid_to=valid_to,
    )


def test_lookup_basic():
    cat = PriceCatalog([_entry("gpt-5-mini", PriceType.INPUT, "0.25")])
    hit = cat.lookup("openai", "gpt-5-mini", PriceType.INPUT, at=date(2026, 6, 1))
    assert hit and hit.price_per_unit == Decimal("0.25")


def test_lookup_time_window():
    cat = PriceCatalog(
        [
            _entry("m", PriceType.INPUT, "1.00", version="v1",
                   valid_from=date(2026, 1, 1), valid_to=date(2026, 5, 1)),
            _entry("m", PriceType.INPUT, "2.00", version="v2", valid_from=date(2026, 5, 1)),
        ]
    )
    old = cat.lookup("openai", "m", PriceType.INPUT, at=date(2026, 3, 1))
    new = cat.lookup("openai", "m", PriceType.INPUT, at=date(2026, 6, 1))
    assert old.version == "v1" and new.version == "v2"


def test_lookup_miss_returns_none():
    cat = PriceCatalog()
    assert cat.lookup("openai", "ghost", PriceType.INPUT, at=date(2026, 6, 1)) is None


def test_tenant_override_precedence():
    cat = PriceCatalog([_entry("gpt-5", PriceType.INPUT, "2.50")])
    cat.add_override("tenant-x", _entry("gpt-5", PriceType.INPUT, "1.00", version="contract"))
    public = cat.lookup("openai", "gpt-5", PriceType.INPUT, at=date(2026, 6, 1))
    contract = cat.lookup(
        "openai", "gpt-5", PriceType.INPUT, at=date(2026, 6, 1), tenant_id="tenant-x"
    )
    assert public.price_per_unit == Decimal("2.50")
    assert contract.price_per_unit == Decimal("1.00")


def test_compute_cost_chat():
    cat = seed_catalog()
    # 1M input + 1M output on gpt-5-mini = 0.25 + 2.00 = 2.25 USD = 2_250_000 micro
    micro, version = compute_cost_micro_usd(
        cat, "openai", "gpt-5-mini",
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        at=date(2026, 6, 1),
    )
    assert micro == 2_250_000
    assert version == "seed-2026-06-15"


def test_compute_cost_cached_input_cheaper():
    cat = seed_catalog()
    # 1M input of which 1M cached, 0 output. cached rate 0.025 → 25_000 micro
    micro, _ = compute_cost_micro_usd(
        cat, "openai", "gpt-5-mini",
        Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000),
        at=date(2026, 6, 1),
    )
    assert micro == 25_000


def test_strict_raises_on_miss():
    cat = PriceCatalog()
    with pytest.raises(PriceCatalogMiss):
        compute_cost_micro_usd(
            cat, "openai", "ghost", Usage(input_tokens=10), at=date(2026, 6, 1), strict=True
        )


def test_non_strict_partial_price():
    # only input priced; output missing → cost from input only, no crash
    cat = PriceCatalog([_entry("m", PriceType.INPUT, "1.00")])
    micro, _ = compute_cost_micro_usd(
        cat, "openai", "m", Usage(input_tokens=1_000_000, output_tokens=500),
        at=date(2026, 6, 1),
    )
    assert micro == 1_000_000


def test_seed_catalog_has_openai():
    cat = seed_catalog()
    assert cat.lookup("openai", "gpt-5", PriceType.OUTPUT, at=date(2026, 6, 1)) is not None
