# SPDX-License-Identifier: Apache-2.0
from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import PriceCatalog, seed_catalog
from tally.schema import GenAI, SpanFields, build_span_attributes


def _span(client_cost=None, model="gpt-5-mini", inp=1_000_000, out=1_000_000):
    fields = SpanFields(
        system="openai",
        request_model=model,
        response_model=model,
        operation="chat",
        input_tokens=inp,
        output_tokens=out,
        cost_estimated_micro_usd=client_cost,
    )
    return build_span_attributes(fields)


AT = date(2026, 6, 1)


def test_server_value_overwrites_and_sets_version():
    res = enrich_cost(_span(client_cost=999), seed_catalog(), at=AT)
    assert res.server_cost_micro_usd == 2_250_000  # 0.25 + 2.00 USD
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 2_250_000
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "seed-2026-06-15"
    assert res.catalog_miss is False


def test_client_cost_is_hint_only():
    # client claimed a wildly different cost; server value still wins
    res = enrich_cost(_span(client_cost=10), seed_catalog(), at=AT)
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == res.server_cost_micro_usd
    assert res.client_cost_micro_usd == 10


def test_drift_flagged_over_threshold():
    res = enrich_cost(_span(client_cost=10), seed_catalog(), at=AT)  # ~100% off
    assert res.drift is not None and res.drift > 0.05
    assert res.drift_exceeded is True


def test_drift_within_threshold_not_flagged():
    # client within 5% of the 2_250_000 server value
    res = enrich_cost(_span(client_cost=2_200_000), seed_catalog(), at=AT)
    assert res.drift_exceeded is False


def test_no_client_cost_no_drift():
    res = enrich_cost(_span(client_cost=None), seed_catalog(), at=AT)
    assert res.client_cost_micro_usd is None
    assert res.drift is None
    assert res.drift_exceeded is False


def test_catalog_miss_removes_cost():
    res = enrich_cost(_span(client_cost=500, model="ghost-model"), seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_empty_catalog_is_miss():
    res = enrich_cost(_span(client_cost=500), PriceCatalog(), at=AT)
    assert res.catalog_miss is True
    assert res.server_cost_micro_usd is None


def test_cached_input_priced_via_catalog():
    fields = SpanFields(
        system="openai", request_model="gpt-5-mini", response_model="gpt-5-mini",
        operation="chat", input_tokens=1_000_000, output_tokens=0,
        cached_input_tokens=1_000_000,
    )
    res = enrich_cost(build_span_attributes(fields), seed_catalog(), at=AT)
    assert res.server_cost_micro_usd == 25_000  # all cached at 0.025 USD


def test_original_not_mutated():
    span = _span(client_cost=10)
    before = dict(span)
    enrich_cost(span, seed_catalog(), at=AT)
    assert span == before  # enrich returns a copy


# CTO-244: a known model with unknown usage must land unpriced, not as a confident $0.
# compute_cost_micro_usd prices whatever it is handed, so coercing absent token counts to 0 here
# produced EstimatedCost = 0 with CostSource = 'estimated' for a real, billed call. See
# _usage_or_none.


def test_known_model_absent_usage_is_unpriced_not_zero():
    span = build_span_attributes(
        SpanFields(system="openai", response_model="gpt-4o-mini", operation="chat")
    )
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.usage_unknown is True
    assert res.server_cost_micro_usd is None
    # No cost claim at all on the span, so mapping writes NULL / CostSource = 'unpriced'.
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes
    assert GenAI.COST_PRICE_CATALOG_VERSION not in res.attributes


def test_regression_gpt_4o_mini_chat_no_usage_does_not_fabricate_zero():
    # The exact call the end-to-end integration test proved still fabricated a priced $0.
    res = enrich_cost(
        {
            GenAI.SYSTEM: "openai",
            GenAI.RESPONSE_MODEL: "gpt-4o-mini",
            GenAI.OPERATION_NAME: "chat",
        },
        seed_catalog(),
    )
    assert res.server_cost_micro_usd is None
    assert res.attributes.get(GenAI.COST_ESTIMATED_MICRO_USD) is None
    assert res.attributes.get(GenAI.COST_PRICE_CATALOG_VERSION) is None


def test_provider_reported_zero_tokens_prices_as_a_real_zero():
    # A reported 0 is a number, not an absence: it still prices, and it prices to 0.
    res = enrich_cost(_span(inp=0, out=0), seed_catalog(), at=AT)
    assert res.usage_unknown is False
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd == 0
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 0
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "seed-2026-06-15"


def test_half_reported_usage_is_still_unknown():
    # Output absent with input known would price the output side at 0, understating a real call.
    span = build_span_attributes(
        SpanFields(
            system="openai", response_model="gpt-5-mini", operation="chat", input_tokens=500
        )
    )
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.usage_unknown is True
    assert res.server_cost_micro_usd is None


def test_unparseable_usage_is_unknown_not_zero():
    span: dict[str, object] = {
        GenAI.SYSTEM: "openai",
        GenAI.RESPONSE_MODEL: "gpt-5-mini",
        GenAI.USAGE_INPUT_TOKENS: "lots",
        GenAI.USAGE_OUTPUT_TOKENS: None,
    }
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.usage_unknown is True
    assert res.server_cost_micro_usd is None


def test_unknown_usage_is_not_reported_as_a_catalog_miss():
    # Two different failures: we HAD a rate, we did not have counts to apply it to.
    span = build_span_attributes(
        SpanFields(system="openai", response_model="gpt-5-mini", operation="chat")
    )
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.usage_unknown is True
    assert res.catalog_miss is False


def test_client_cost_still_surfaces_when_usage_is_unknown():
    span = build_span_attributes(
        SpanFields(
            system="openai",
            response_model="gpt-5-mini",
            operation="chat",
            cost_estimated_micro_usd=1234,
        )
    )
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.client_cost_micro_usd == 1234
    assert res.server_cost_micro_usd is None
    assert res.drift is None
