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


# --- per-call layers: tools and vector (CTO-243) ------------------------------------------------
#
# The SDK carries these costs on gen_ai.tool.cost_micro_usd. Nothing promoted that into the
# canonical cost attribute, so the tools and vector layers reported zero spend.


def _tool_span(client_cost=10_000, provider="tavily", tool="search"):
    return build_span_attributes(
        SpanFields(
            system=provider,
            operation="tool",
            tool_name=tool,
            tool_call_id="call-1",
            tool_cost_micro_usd=client_cost,
        )
    )


def _vector_span(client_cost=400, provider="pinecone", index="docs", operation="query"):
    return build_span_attributes(
        SpanFields(
            system=provider,
            operation="vector",
            tool_name=f"{provider}.{index}.{operation}",
            tool_cost_micro_usd=client_cost,
        )
    )


def test_tool_cost_is_promoted_to_the_canonical_cost_key():
    res = enrich_cost(_tool_span(), seed_catalog(), at=AT)
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 10_000  # tavily search, 0.01 USD
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "seed-2026-06-15"
    assert res.catalog_miss is False


def test_vector_cost_is_priced_off_the_operation_segment():
    res = enrich_cost(_vector_span(), seed_catalog(), at=AT)
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 400  # pinecone query, 0.0004 USD
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "seed-2026-06-15"


def test_vector_index_name_containing_dots_still_prices():
    res = enrich_cost(_vector_span(index="docs.v2"), seed_catalog(), at=AT)
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 400


def test_call_cost_server_catalog_wins_and_drift_is_flagged():
    res = enrich_cost(_tool_span(client_cost=1), seed_catalog(), at=AT)
    assert res.server_cost_micro_usd == 10_000
    assert res.client_cost_micro_usd == 1
    assert res.drift_exceeded is True


def test_call_cost_survives_a_catalog_miss():
    # A negotiated per-call rate the catalog has no entry for is kept, not discarded as zero.
    res = enrich_cost(_tool_span(provider="acme-internal", tool="lookup"), seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 10_000
    assert res.server_cost_micro_usd is None
    assert GenAI.COST_PRICE_CATALOG_VERSION not in res.attributes


def test_unpriced_call_asserts_no_cost():
    span = _tool_span(client_cost=None, provider="acme-internal", tool="lookup")
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes
    assert res.client_cost_micro_usd is None


def test_call_cost_original_not_mutated():
    span = _tool_span()
    before = dict(span)
    enrich_cost(span, seed_catalog(), at=AT)
    assert span == before
