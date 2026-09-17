# SPDX-License-Identifier: Apache-2.0
"""Fireworks AI seed catalog coverage (CTO-418).

The provider string on the wire is ``fireworks-ai``, not ``fireworks``, and the model slot holds
the full account-scoped id. Both are asserted here against the real ``enrich_cost`` path, because
a key that does not match the telemetry prices nothing while looking perfectly correct in review.
"""

from __future__ import annotations

from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import PriceType, Unit, seed_catalog
from tally.schema import GenAI

AT = date(2026, 9, 17)

PROVIDER = "fireworks-ai"
KIMI = "accounts/fireworks/models/kimi-k3"
QWEN_A95B = "accounts/fireworks/models/qwen3p8-2p4t-a95b"
QWEN_PLUS = "accounts/fireworks/models/qwen3p7-plus"


def _span(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int | None = None,
) -> dict[str, object]:
    attrs: dict[str, object] = {
        GenAI.SYSTEM: provider,
        GenAI.OPERATION_NAME: "chat",
        GenAI.REQUEST_MODEL: model,
        GenAI.RESPONSE_MODEL: model,
        GenAI.USAGE_INPUT_TOKENS: input_tokens,
        GenAI.USAGE_OUTPUT_TOKENS: output_tokens,
    }
    if cached_input_tokens is not None:
        attrs[GenAI.USAGE_CACHED_INPUT_TOKENS] = cached_input_tokens
    return attrs


def test_kimi_k3_prices_non_zero_through_enrich_cost() -> None:
    res = enrich_cost(_span(PROVIDER, KIMI, 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.usage_unknown is False
    assert res.server_cost_micro_usd is not None
    # 1000 in @ $3.00/MTok + 250 out @ $15.00/MTok = $0.00675.
    assert res.server_cost_micro_usd == 6_750


def test_qwen3p8_prices_non_zero_through_enrich_cost() -> None:
    res = enrich_cost(_span(PROVIDER, QWEN_A95B, 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.usage_unknown is False
    assert res.server_cost_micro_usd is not None
    # 1000 in @ $2.00/MTok + 250 out @ $6.00/MTok = $0.0035.
    assert res.server_cost_micro_usd == 3_500


def test_fireworks_entries_use_the_unit_enum_not_a_bare_string() -> None:
    """A string in ``PriceEntry.unit`` is accepted and then silently prices at ZERO.

    ``_line`` matches ``entry.unit`` with ``is`` against :class:`Unit`, so an entry carrying the
    plain string ``"per_million_tokens"`` falls through to ``Decimal(0)`` instead of raising. That
    failure is invisible in review, so pin the unit on the new entries.
    """
    cat = seed_catalog()
    for model in (KIMI, QWEN_A95B):
        for price_type in (PriceType.INPUT, PriceType.OUTPUT):
            entry = cat.lookup(PROVIDER, model, price_type, at=AT)
            assert entry is not None, f"no {price_type} entry for {model}"
            assert entry.unit is Unit.PER_MILLION_TOKENS


def test_provider_fireworks_without_the_ai_suffix_still_misses() -> None:
    """The key is ``fireworks-ai``. Nothing was keyed under the shorter spelling by accident."""
    res = enrich_cost(_span("fireworks", KIMI, 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert res.server_cost_micro_usd is None
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_qwen3p7_plus_stays_deliberately_unpriced() -> None:
    """No public rate could be confirmed, so it renders blank rather than a guessed number."""
    res = enrich_cost(_span(PROVIDER, QWEN_PLUS, 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert res.server_cost_micro_usd is None
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_pilot_actual_usage_totals() -> None:
    """The pilot's real observed usage, so the owner can check these against a real invoice.

    kimi-k3: 155,332 in @ $3.00 + 925 out @ $15.00 = $0.479871 (479_871 micro-USD).
    qwen3p8-2p4t-a95b: 139,625 in @ $2.00 + 1,901 out @ $6.00 = $0.290656 (290_656 micro-USD).
    """
    cat = seed_catalog()

    kimi = enrich_cost(_span(PROVIDER, KIMI, 155_332, 925), cat, at=AT)
    assert kimi.server_cost_micro_usd == 479_871

    qwen = enrich_cost(_span(PROVIDER, QWEN_A95B, 139_625, 1_901), cat, at=AT)
    assert qwen.server_cost_micro_usd == 290_656


def test_cached_input_tokens_bill_at_the_full_input_rate() -> None:
    """No cached rate was supplied, so cached tokens cost the same as uncached ones.

    This documents a known OVERSTATEMENT for cache-heavy Fireworks traffic rather than hiding it:
    ``compute_cost_micro_usd`` falls back to the standard input rate when no CACHED_INPUT tier
    resolves. Fixing it means sourcing the real cached rates, not inventing a discount.
    """
    cat = seed_catalog()
    assert cat.lookup(PROVIDER, KIMI, PriceType.CACHED_INPUT, at=AT) is None

    plain = enrich_cost(_span(PROVIDER, KIMI, 10_000, 100), cat, at=AT)
    cached = enrich_cost(_span(PROVIDER, KIMI, 10_000, 100, cached_input_tokens=8_000), cat, at=AT)
    assert cached.server_cost_micro_usd == plain.server_cost_micro_usd
