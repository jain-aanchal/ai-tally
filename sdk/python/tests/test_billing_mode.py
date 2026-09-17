# SPDX-License-Identifier: Apache-2.0
"""Subscription-billed spans are never priced at API list rates (CTO-417).

ai-tally prices from (provider, model, tokens) and had no notion of HOW a call was billed, so a
call covered by a seat or plan whose model happens to sit in the catalog was priced at
pay-as-you-go list rates and stamped ``CostSource = 'estimated'``: a confident assertion of a cost
the customer never incurred.

The case reproduced here is real. A pilot tenant, 90 days: 181
subscription calls escaped pricing only because their model ids missed the catalog, and one
``openai`` / ``gpt-4o-mini`` call did not miss, so it was priced. That one call is
``test_reproduces_the_priced_gpt_4o_mini_subscription_call``.
"""

from __future__ import annotations

from datetime import date

from tally.client import TallyClient
from tally.enrichment import enrich_cost
from tally.pricing import Usage, compute_cost_micro_usd, seed_catalog
from tally.schema import GenAI, SpanFields, build_span_attributes, validate_span_attributes

AT = date(2026, 6, 1)

# The production call, attribute for attribute: 12 input tokens, 5 output, provider openai,
# model gpt-4o-mini, a chat completion.
_REAL_CALL: dict[str, object] = {
    GenAI.SYSTEM: "openai",
    GenAI.REQUEST_MODEL: "gpt-4o-mini",
    GenAI.OPERATION_NAME: "chat",
    GenAI.USAGE_INPUT_TOKENS: 12,
    GenAI.USAGE_OUTPUT_TOKENS: 5,
}


def test_the_catalog_really_does_price_this_call() -> None:
    """Guards the premise of every test below: without it they would pass vacuously.

    If the seed catalog ever stops pricing gpt-4o-mini, a subscription span would land unpriced for
    the wrong reason and the tests asserting "no cost" would still be green while proving nothing.
    """
    cost, version = compute_cost_micro_usd(
        seed_catalog(), "openai", "gpt-4o-mini", Usage(input_tokens=12, output_tokens=5), at=AT
    )
    assert cost > 0
    assert version


def test_reproduces_the_priced_gpt_4o_mini_subscription_call() -> None:
    """The bug, and the fix, on the one call that actually fired in production.

    Unmarked (what that producer sends today) the call prices at list rates. Marked subscription it
    carries no cost at all, though nothing else about it changed.
    """
    before = enrich_cost(dict(_REAL_CALL), seed_catalog(), at=AT)
    assert before.server_cost_micro_usd == 5  # micro-USD, the figure the customer never incurred
    assert before.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 5

    after = enrich_cost(
        {**_REAL_CALL, GenAI.COST_BILLING_MODE: "subscription"}, seed_catalog(), at=AT
    )
    assert after.subscription_billed is True
    assert after.server_cost_micro_usd is None
    assert GenAI.COST_ESTIMATED_MICRO_USD not in after.attributes
    assert GenAI.COST_PRICE_CATALOG_VERSION not in after.attributes


def test_absent_billing_mode_is_byte_for_byte_todays_behaviour() -> None:
    """The additive-only wire contract (CTO-31). An OLD producer sends nothing and is unaffected.

    The span here is shaped exactly as a pre-CTO-417 producer posts one: no billing mode key at all.
    Absence must mean "price it as we do now", never "unknown, refuse to price", or this change
    silently stops pricing every producer already in the field.
    """
    span = build_span_attributes(
        SpanFields(
            system="openai",
            request_model="gpt-4o-mini",
            response_model="gpt-4o-mini",
            operation="chat",
            input_tokens=12,
            output_tokens=5,
        )
    )
    assert GenAI.COST_BILLING_MODE not in span

    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.subscription_billed is False
    assert res.billing_mode_unknown is False
    assert res.catalog_miss is False
    assert res.usage_unknown is False
    assert res.server_cost_micro_usd == 5
    assert res.attributes[GenAI.COST_ESTIMATED_MICRO_USD] == 5
    assert res.attributes[GenAI.COST_PRICE_CATALOG_VERSION] == "seed-2026-06-15"


def test_explicit_api_mode_prices_identically_to_absence() -> None:
    absent = enrich_cost(dict(_REAL_CALL), seed_catalog(), at=AT)
    explicit = enrich_cost({**_REAL_CALL, GenAI.COST_BILLING_MODE: "api"}, seed_catalog(), at=AT)
    assert explicit.server_cost_micro_usd == absent.server_cost_micro_usd
    assert (
        explicit.attributes[GenAI.COST_PRICE_CATALOG_VERSION]
        == absent.attributes[GenAI.COST_PRICE_CATALOG_VERSION]
    )


def test_subscription_withholds_only_the_cost_everything_else_still_flows() -> None:
    """Usage, model, feature tag and attribution must be unaffected. Only the money is withheld."""
    span = {
        **_REAL_CALL,
        GenAI.COST_BILLING_MODE: "subscription",
        GenAI.FEATURE_TAG: "search",
        GenAI.SESSION_ID: "sess-1",
        GenAI.ACCOUNT_ID_HASH: "a" * 64,
        GenAI.USAGE_CACHED_INPUT_TOKENS: 3,
    }
    out = enrich_cost(span, seed_catalog(), at=AT).attributes
    assert out[GenAI.USAGE_INPUT_TOKENS] == 12
    assert out[GenAI.USAGE_OUTPUT_TOKENS] == 5
    assert out[GenAI.USAGE_CACHED_INPUT_TOKENS] == 3
    assert out[GenAI.REQUEST_MODEL] == "gpt-4o-mini"
    assert out[GenAI.SYSTEM] == "openai"
    assert out[GenAI.FEATURE_TAG] == "search"
    assert out[GenAI.SESSION_ID] == "sess-1"
    assert out[GenAI.ACCOUNT_ID_HASH] == "a" * 64
    # The marker itself survives onto the span, so a reader can see why the cost is missing without
    # having to infer it from CostSource alone.
    assert out[GenAI.COST_BILLING_MODE] == "subscription"


def test_subscription_drops_a_client_asserted_cost_too() -> None:
    """A producer contradicting itself must not leave list-rate spend on the row by the back door.

    It declared the call subscription-billed and also reported a cost. Keeping the number would put
    the same fabricated spend back, just by another route.
    """
    res = enrich_cost(
        {**_REAL_CALL, GenAI.COST_BILLING_MODE: "subscription", GenAI.COST_ESTIMATED_MICRO_USD: 5},
        seed_catalog(),
        at=AT,
    )
    assert res.client_cost_micro_usd == 5  # reported back, for drift diagnosis
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_subscription_also_vetoes_the_per_call_priced_branch() -> None:
    """The guard sits ahead of the tool/vector branch, not only the token one."""
    res = enrich_cost(
        {
            GenAI.SYSTEM: "openai",
            GenAI.OPERATION_NAME: "tool",
            GenAI.TOOL_NAME: "web_search",
            GenAI.TOOL_COST_MICRO_USD: 10_000,
            GenAI.COST_BILLING_MODE: "subscription",
        },
        seed_catalog(),
        at=AT,
    )
    assert res.subscription_billed is True
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes
    assert GenAI.TOOL_COST_MICRO_USD not in res.attributes


def test_unrecognised_mode_is_not_priced_and_is_not_called_subscription() -> None:
    """We do not know how it was billed, so we assert neither a cost nor a billing story.

    Pricing it at list rates would be the original bug; calling it 'subscription' would invent a
    fact we were not told. It lands unpriced.
    """
    res = enrich_cost(
        {**_REAL_CALL, GenAI.COST_BILLING_MODE: "prepaid-credits"}, seed_catalog(), at=AT
    )
    assert res.billing_mode_unknown is True
    assert res.subscription_billed is False
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_mode_is_case_and_whitespace_tolerant() -> None:
    res = enrich_cost(
        {**_REAL_CALL, GenAI.COST_BILLING_MODE: "  Subscription "}, seed_catalog(), at=AT
    )
    assert res.subscription_billed is True
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes


def test_enrich_does_not_mutate_the_posted_span() -> None:
    span = {**_REAL_CALL, GenAI.COST_BILLING_MODE: "subscription"}
    before = dict(span)
    enrich_cost(span, seed_catalog(), at=AT)
    assert span == before


# --- producer surface ---------------------------------------------------------------------------


def test_validator_accepts_the_two_modes_and_names_a_typo() -> None:
    for mode in ("api", "subscription"):
        assert validate_span_attributes({GenAI.COST_BILLING_MODE: mode}) == []
    violations = validate_span_attributes({GenAI.COST_BILLING_MODE: "subscriptions"})
    assert len(violations) == 1
    assert GenAI.COST_BILLING_MODE in violations[0]


def test_per_call_mode_beats_the_client_default_in_both_directions() -> None:
    """The case that forced a per-span field: one process, both billing modes.

    The motivating tenant's Fireworks traffic is real per-token API spend while their openai and
    claude-code traffic runs under a subscription, so neither a per-tenant nor a per-provider switch
    could have expressed it.
    """
    subscribed = TallyClient(catalog=seed_catalog(), billing_mode="subscription")
    covered = subscribed.record_llm_call(
        provider="openai", model="gpt-4o-mini", usage=Usage(input_tokens=12, output_tokens=5)
    )
    assert covered.cost_micro_usd is None
    assert covered.attributes[GenAI.COST_BILLING_MODE] == "subscription"

    # ... and the same client's genuinely per-token traffic still prices.
    metered = subscribed.record_llm_call(
        provider="openai",
        model="gpt-4o-mini",
        usage=Usage(input_tokens=12, output_tokens=5),
        billing_mode="api",
    )
    assert metered.cost_micro_usd == 5
    assert metered.attributes[GenAI.COST_BILLING_MODE] == "api"


def test_client_without_a_mode_emits_no_marker_at_all() -> None:
    """A caller that never heard of CTO-417 keeps posting exactly the span it posted before."""
    result = TallyClient(catalog=seed_catalog()).record_llm_call(
        provider="openai", model="gpt-4o-mini", usage=Usage(input_tokens=12, output_tokens=5)
    )
    assert GenAI.COST_BILLING_MODE not in result.attributes
    assert result.cost_micro_usd == 5


def test_subscription_embedding_is_not_priced_client_side() -> None:
    result = TallyClient(catalog=seed_catalog()).record_embedding_call(
        provider="openai",
        model="text-embedding-3-small",
        input_tokens=1_000,
        billing_mode="subscription",
    )
    assert result.cost_micro_usd is None
    assert result.attributes[GenAI.COST_BILLING_MODE] == "subscription"
    # Usage still lands: only the cost is withheld.
    assert result.attributes[GenAI.USAGE_INPUT_TOKENS] == 1_000


def test_span_fields_round_trip_the_mode() -> None:
    attrs = build_span_attributes(
        SpanFields(system="openai", request_model="gpt-4o-mini", billing_mode="subscription")
    )
    assert attrs[GenAI.COST_BILLING_MODE] == "subscription"
    assert validate_span_attributes(attrs) == []
