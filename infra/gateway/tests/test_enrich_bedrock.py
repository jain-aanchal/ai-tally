# SPDX-License-Identifier: Apache-2.0
"""Gateway cost enrichment for Amazon Bedrock spans (CTO-157).

The gateway recomputes cost from the catalog in ``enrich_cost``. A span with
``gen_ai.system="bedrock"`` and a seeded Bedrock model must price to a non-zero cost with
no ``catalog_miss`` — proving Bedrock needs no special-casing in the mapping/enrich path
(provider is just the catalog lookup key), exactly like the CTO-149 Gemini case.
"""

from __future__ import annotations

from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import seed_catalog
from tally.schema import GenAI

from gateway.mapping import COLUMNS, span_to_row

AT = date(2026, 6, 1)


def _span(model: str, input_tokens: int = 1000, output_tokens: int = 250) -> dict[str, object]:
    return {
        GenAI.SYSTEM: "bedrock",
        GenAI.REQUEST_MODEL: model,
        GenAI.RESPONSE_MODEL: model,
        GenAI.USAGE_INPUT_TOKENS: input_tokens,
        GenAI.USAGE_OUTPUT_TOKENS: output_tokens,
    }


def test_enrich_bedrock_claude_no_catalog_miss() -> None:
    res = enrich_cost(_span("anthropic.claude-sonnet-4-5"), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enrich_bedrock_nova_no_catalog_miss() -> None:
    res = enrich_cost(_span("amazon.nova-pro"), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enrich_bedrock_llama_no_catalog_miss() -> None:
    res = enrich_cost(_span("meta.llama3-3-70b-instruct"), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enriched_bedrock_span_maps_to_row_with_cost() -> None:
    # End-to-end through the mapping: enriched bedrock span -> otel row with a non-zero
    # EstimatedCost and GenAiSystem="bedrock", no special-casing anywhere.
    res = enrich_cost(
        _span("anthropic.claude-sonnet-4-5", 1_000_000, 1_000_000), seed_catalog(), at=AT
    )
    row = span_to_row(res.attributes, tenant_id="tn-1", effective_ts_ns=1_700_000_000_000_000_000)
    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["GenAiSystem"] == "bedrock"
    assert by_col["GenAiRequestModel"] == "anthropic.claude-sonnet-4-5"
    assert by_col["EstimatedCost"] > 0
