# SPDX-License-Identifier: Apache-2.0
"""Gateway cost enrichment for Vercel-AI-Gateway-proxied spans (CTO-161).

A call routed through the Vercel AI Gateway is resolved SDK-side to its TRUE upstream
``(provider, model)`` (see :mod:`tally.vercel_gateway`), so by the time the span reaches the
gateway it carries ``gen_ai.system="openai"`` / ``gen_ai.request.model="gpt-4o-mini"`` — and
``enrich_cost`` prices it straight from the catalog with no special-casing, exactly like a
direct SDK call (the CTO-149 / CTO-157 pattern). An unresolved upstream carries
``gen_ai.system="unknown"``, which the catalog can't price → ``catalog_miss`` → cost null.
"""

from __future__ import annotations

from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import seed_catalog
from tally.schema import GenAI
from tally.vercel_gateway import resolve_upstream

from gateway.mapping import COLUMNS, span_to_row

AT = date(2026, 6, 1)

# The same captured Vercel-AI-Gateway response the SDK test uses, minus bodies.
_GATEWAY_OPENAI_RESPONSE = {
    "model": "openai/gpt-4o-mini",
    "usage": {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 0},
    },
}


def _span_from_gateway(md: dict[str, object]) -> dict[str, object]:
    """Build the span the SDK would emit for a gateway-proxied call from its response metadata."""
    att = resolve_upstream(md)
    return {
        GenAI.SYSTEM: att.provider,
        GenAI.REQUEST_MODEL: att.model,
        GenAI.RESPONSE_MODEL: att.model,
        GenAI.USAGE_INPUT_TOKENS: att.usage.input_tokens,
        GenAI.USAGE_OUTPUT_TOKENS: att.usage.output_tokens,
        GenAI.USAGE_CACHED_INPUT_TOKENS: att.usage.cached_input_tokens,
    }


def test_enrich_gateway_openai_prices_true_upstream() -> None:
    res = enrich_cost(_span_from_gateway(_GATEWAY_OPENAI_RESPONSE), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enrich_gateway_matches_direct_openai_cost() -> None:
    # The gateway hop must not change the price: enriching the resolved span equals enriching a
    # direct openai/gpt-4o-mini span with the same usage.
    gateway = enrich_cost(_span_from_gateway(_GATEWAY_OPENAI_RESPONSE), seed_catalog(), at=AT)
    direct_span = {
        GenAI.SYSTEM: "openai",
        GenAI.REQUEST_MODEL: "gpt-4o-mini",
        GenAI.RESPONSE_MODEL: "gpt-4o-mini",
        GenAI.USAGE_INPUT_TOKENS: 1_000_000,
        GenAI.USAGE_OUTPUT_TOKENS: 1_000_000,
    }
    direct = enrich_cost(direct_span, seed_catalog(), at=AT)
    assert gateway.server_cost_micro_usd == direct.server_cost_micro_usd


def test_enriched_gateway_span_maps_to_row_with_true_upstream() -> None:
    res = enrich_cost(_span_from_gateway(_GATEWAY_OPENAI_RESPONSE), seed_catalog(), at=AT)
    row = span_to_row(res.attributes, tenant_id="tn-1", effective_ts_ns=1_700_000_000_000_000_000)
    by_col = dict(zip(COLUMNS, row, strict=True))
    # True upstream lands in the typed columns — NOT "vercel".
    assert by_col["GenAiSystem"] == "openai"
    assert by_col["GenAiRequestModel"] == "gpt-4o-mini"
    assert by_col["EstimatedCost"] > 0


def test_enrich_gateway_unknown_upstream_is_catalog_miss() -> None:
    # Gateway metadata with no nameable model -> resolved to "unknown" -> the catalog can't price
    # it, so the cost is dropped (dashboard "—"), never fabricated.
    span = _span_from_gateway({"usage": {"prompt_tokens": 1000, "completion_tokens": 250}})
    assert span[GenAI.SYSTEM] == "unknown"
    res = enrich_cost(span, seed_catalog(), at=AT)
    assert res.catalog_miss is True
    assert res.server_cost_micro_usd is None
    assert GenAI.COST_ESTIMATED_MICRO_USD not in res.attributes
