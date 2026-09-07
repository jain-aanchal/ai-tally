# SPDX-License-Identifier: Apache-2.0
from datetime import date
from decimal import Decimal

from tally.enrichment import enrich_cost
from tally.pricing import (
    PriceCatalog,
    PriceEntry,
    PriceType,
    Unit,
    compute_call_cost_micro_usd,
    seed_catalog,
)
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


# CTO-244: the usage check is per operation, because the layers are billed differently. A single
# both-sides rule silently un-prices every embedding span (no output side by design) and every
# per-call layer (no token counts at all). These cases pin each branch so nobody "simplifies" the
# check back to one shape. They mirror db/clickhouse/rollups.sql's UnknownUsageSpanCount predicate.


def _embedding_input_rate_catalog() -> PriceCatalog:
    # An input-token rate for an embedding model. On this branch enrich_cost still resolves cost
    # through the token pricer, so this is what proves the span reaches the pricer at all instead
    # of being vetoed as unknown-usage. Once the sibling embedding routing lands, the same span
    # resolves through the EMBEDDING tier; either way it must PRICE, not land unpriced.
    cat = PriceCatalog()
    cat.add(
        PriceEntry(
            version="test-embed",
            valid_from=date(2026, 1, 1),
            provider="openai",
            model="text-embedding-3-small",
            price_type=PriceType.INPUT,
            unit=Unit.PER_MILLION_TOKENS,
            price_per_unit=Decimal("0.02"),
        )
    )
    return cat


def _embedding_span(inp: int | None = 1_000_000) -> dict[str, object]:
    fields = SpanFields(
        system="openai",
        request_model="text-embedding-3-small",
        response_model="text-embedding-3-small",
        operation="embeddings",
        input_tokens=inp,
    )
    return build_span_attributes(fields)


def test_embeddings_with_input_only_is_not_unknown_usage():
    # The regression this whole change exists to prevent: an embedding span carries no output
    # count by design, so a both-sides rule marked every one of them unknown-usage.
    res = enrich_cost(_embedding_span(), seed_catalog(), at=AT)
    assert res.usage_unknown is False


def test_embeddings_with_input_only_prices():
    res = enrich_cost(_embedding_span(), _embedding_input_rate_catalog(), at=AT)
    assert res.usage_unknown is False
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd == 20_000  # 1M tokens at 0.02 USD/Mtok
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 20_000
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "test-embed"


def test_embeddings_without_input_tokens_is_unknown_usage():
    # Input is the one side an embedding call does have, so an absent one really is unknown.
    res = enrich_cost(_embedding_span(inp=None), _embedding_input_rate_catalog(), at=AT)
    assert res.usage_unknown is True
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is None
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes
    assert GenAI.COST_PRICE_CATALOG_VERSION not in res.attributes


def _per_call_span(operation: str, provider: str, name: str, client_cost=None):
    fields = SpanFields(
        system=provider,
        request_model=name,
        response_model=name,
        operation=operation,
        cost_estimated_micro_usd=client_cost,
    )
    return build_span_attributes(fields)


def test_tool_span_without_tokens_is_never_unknown_usage():
    # Priced per call from the catalog, so there is no token usage to be unknown about.
    res = enrich_cost(_per_call_span("tool", "tavily", "search"), seed_catalog(), at=AT)
    assert res.usage_unknown is False


def test_vector_span_without_tokens_is_never_unknown_usage():
    res = enrich_cost(_per_call_span("vector", "pinecone", "query"), seed_catalog(), at=AT)
    assert res.usage_unknown is False


def test_per_call_layers_price_per_call_from_the_catalog():
    # The per-call rates the sibling branch's enrich_cost branch resolves against. Asserted here so
    # the write-side rule and the catalog cannot drift apart: tavily/search and pinecone/query are
    # priced per CALL, with no token tiers to fall back on.
    cat = seed_catalog()
    tool_cost, tool_version = compute_call_cost_micro_usd(
        cat, "tavily", "search", PriceType.TOOL_CALL, at=AT
    )
    assert (tool_cost, bool(tool_version)) == (10_000, True)
    vector_cost, vector_version = compute_call_cost_micro_usd(
        cat, "pinecone", "query", PriceType.VECTOR_CALL, at=AT
    )
    assert (vector_cost, bool(vector_version)) == (400, True)


def test_client_per_call_cost_survives_a_catalog_miss():
    # Honest under uncertainty cuts both ways: we assert no server cost we cannot justify, but the
    # client's figure is still reported rather than thrown away.
    res = enrich_cost(
        _per_call_span("tool", "nosuchvendor", "search", client_cost=12_345),
        seed_catalog(),
        at=AT,
    )
    assert res.usage_unknown is False
    assert res.catalog_miss is True
    assert res.client_cost_micro_usd == 12_345
    assert res.server_cost_micro_usd is None


def test_no_catalog_entry_and_no_client_figure_fabricates_nothing():
    res = enrich_cost(_per_call_span("vector", "nosuchvendor", "query"), seed_catalog(), at=AT)
    assert res.server_cost_micro_usd is None
    assert res.client_cost_micro_usd is None
    assert res.attributes.get(GenAI.COST_ESTIMATED_MICRO_USD) is None
    assert res.attributes.get(GenAI.COST_PRICE_CATALOG_VERSION) is None
