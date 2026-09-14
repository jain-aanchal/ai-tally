# SPDX-License-Identifier: Apache-2.0
"""CTO-368: a dated snapshot id prices against its model family.

Providers report the snapshot they served in ``gen_ai.response.model``: Anthropic returns
``claude-haiku-4-5-20251001``, OpenAI returns ``gpt-4o-mini-2024-07-18``. The catalog lists family
ids, and the lookup matched by exact string, so the first live proxy call landed ``unpriced`` with
its model and tokens known.
"""

from datetime import date
from decimal import Decimal

import pytest

from tally.pricing import (
    PriceCatalog,
    PriceEntry,
    PriceType,
    Unit,
    Usage,
    compute_cost_micro_usd,
    seed_catalog,
)

AT = date(2026, 9, 14)


def _entry(provider, model, pt, rate):
    return PriceEntry(
        version="v1",
        valid_from=date(2026, 1, 1),
        provider=provider,
        model=model,
        price_type=pt,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(rate),
    )


def test_anthropic_dated_id_prices_the_first_live_call():
    # The exact call from the 2026-09-14 live test: 8 input, 16 output on Haiku 4.5.
    # Seed rates are $1.00 / $5.00 per million, so 8 + 80 = 88 micro-USD.
    cost, version = compute_cost_micro_usd(
        seed_catalog(),
        "anthropic",
        "claude-haiku-4-5-20251001",
        Usage(input_tokens=8, output_tokens=16),
        at=AT,
    )
    assert version, "a dated Anthropic id must not be a catalog miss"
    assert cost == 88


def test_openai_dashed_date_id_prices_against_family():
    cat = PriceCatalog([_entry("openai", "gpt-4o-mini", PriceType.INPUT, "0.15")])
    hit = cat.lookup("openai", "gpt-4o-mini-2024-07-18", PriceType.INPUT, at=AT)
    assert hit is not None and hit.model == "gpt-4o-mini"


def test_exact_dated_entry_wins_over_family():
    # A snapshot priced differently from its family can be listed on its own, and must win.
    cat = PriceCatalog(
        [
            _entry("openai", "gpt-4o", PriceType.INPUT, "2.50"),
            _entry("openai", "gpt-4o-2024-05-13", PriceType.INPUT, "5.00"),
        ]
    )
    hit = cat.lookup("openai", "gpt-4o-2024-05-13", PriceType.INPUT, at=AT)
    assert hit is not None and hit.price_per_unit == Decimal("5.00")


def test_tenant_family_override_beats_public_dated_entry():
    # A contract on the family is what the tenant pays for every snapshot of it.
    cat = PriceCatalog([_entry("anthropic", "claude-haiku-4-5-20251001", PriceType.INPUT, "1.00")])
    cat.add_override("t1", _entry("anthropic", "claude-haiku-4-5", PriceType.INPUT, "0.80"))
    hit = cat.lookup(
        "anthropic", "claude-haiku-4-5-20251001", PriceType.INPUT, at=AT, tenant_id="t1"
    )
    assert hit is not None and hit.price_per_unit == Decimal("0.80")
    # Another tenant without the contract still gets the public dated price.
    other = cat.lookup(
        "anthropic", "claude-haiku-4-5-20251001", PriceType.INPUT, at=AT, tenant_id="t2"
    )
    assert other is not None and other.price_per_unit == Decimal("1.00")


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4-0613",  # a four-digit MMDD snapshot is not a full date; stays a miss
        "claude-haiku-4-5-20251399",  # eight digits that are not a calendar date
        "claude-haiku-4-5-v2",  # not a date at all
        "20251001",  # nothing left once the date is removed
    ],
)
def test_non_date_suffix_is_not_stripped(model):
    # Honest under uncertainty: only a real calendar date is treated as a snapshot, so anything
    # else stays a miss rather than borrowing a price it may not have.
    cat = PriceCatalog(
        [
            _entry("openai", "gpt-4", PriceType.INPUT, "30.00"),
            _entry("anthropic", "claude-haiku-4-5", PriceType.INPUT, "1.00"),
        ]
    )
    provider = "openai" if model.startswith("gpt") else "anthropic"
    assert cat.lookup(provider, model, PriceType.INPUT, at=AT) is None
