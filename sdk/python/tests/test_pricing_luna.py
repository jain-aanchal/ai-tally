# SPDX-License-Identifier: Apache-2.0
"""gpt-5.6-luna seed catalog coverage (CTO-424).

The cached-read tier carries most of the weight here. Without it, cached tokens fall back to the
standard input rate (compute_cost_micro_usd), which for this model is 10x the real price, so a
cache-heavy agent would report a bill mostly made of that error. The fallback is asserted against
explicitly rather than assumed absent.
"""

from __future__ import annotations

from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import PriceType, seed_catalog
from tally.schema import GenAI

AT = date(2026, 9, 17)

PROVIDER = "openai"
LUNA = "gpt-5.6-luna"


def _span(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int | None = None,
) -> dict[str, object]:
    attrs: dict[str, object] = {
        GenAI.SYSTEM: PROVIDER,
        GenAI.OPERATION_NAME: "chat",
        GenAI.REQUEST_MODEL: model,
        GenAI.RESPONSE_MODEL: model,
        GenAI.USAGE_INPUT_TOKENS: input_tokens,
        GenAI.USAGE_OUTPUT_TOKENS: output_tokens,
    }
    if cached_input_tokens is not None:
        attrs[GenAI.USAGE_CACHED_INPUT_TOKENS] = cached_input_tokens
    return attrs


def test_luna_prices_non_zero_through_enrich_cost() -> None:
    res = enrich_cost(_span(LUNA, 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.usage_unknown is False
    # 1000 in @ $0.20/MTok + 250 out @ $1.20/MTok = $0.0005.
    assert res.server_cost_micro_usd == 500


def test_a_cached_read_is_billed_at_the_cached_tier_not_the_input_rate() -> None:
    """The reason this model needed a CACHED_INPUT entry and not just input/output.

    800 of the 1000 prompt tokens are cache reads. At the cached tier that share costs $0.000016;
    at the standard input rate it would cost $0.00016, ten times more. A seed carrying only input
    and output would silently take the second path and look entirely correct in review.
    """
    res = enrich_cost(_span(LUNA, 1_000, 250, cached_input_tokens=800), seed_catalog(), at=AT)
    # 200 uncached @ $0.20 = $0.00004, 800 cached @ $0.02 = $0.000016, 250 out @ $1.20 = $0.0003.
    assert res.server_cost_micro_usd == 356
    # What the input-rate fallback would have produced, pinned so a dropped cached entry fails here
    # rather than quietly inflating a customer's bill.
    assert res.server_cost_micro_usd != 500


def test_the_cached_tier_is_a_tenth_of_the_input_rate() -> None:
    catalog = seed_catalog()
    inp = catalog.lookup(PROVIDER, LUNA, PriceType.INPUT, at=AT)
    cached = catalog.lookup(PROVIDER, LUNA, PriceType.CACHED_INPUT, at=AT)
    assert inp is not None and cached is not None
    assert cached.price_per_unit * 10 == inp.price_per_unit


def test_a_near_miss_model_spelling_prices_nothing() -> None:
    """Lookup is exact. A model id that differs at all is a catalog miss, not a near match.

    Worth pinning because the failure is invisible: the span stores an honest blank and nothing
    distinguishes "we have no rate" from "the rate is filed under a different spelling".
    """
    res = enrich_cost(_span("gpt-5.6-Luna", 1_000, 250), seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert res.server_cost_micro_usd is None
