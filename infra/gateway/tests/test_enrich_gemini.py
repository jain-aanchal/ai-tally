# SPDX-License-Identifier: Apache-2.0
"""Gateway cost enrichment for Google Gemini spans (CTO-149).

The gateway recomputes cost from the catalog in ``enrich_cost``. A span with
``gen_ai.system="google"`` and a seeded gemini model must price to a non-zero cost with
no ``catalog_miss`` — proving Google needs no special-casing in the mapping/enrich path
(provider is just the catalog lookup key).
"""

from __future__ import annotations

from datetime import date

from tally.enrichment import enrich_cost
from tally.pricing import seed_catalog
from tally.schema import GenAI

from gateway.mapping import span_to_row

AT = date(2026, 6, 1)


def _span(model: str, input_tokens: int = 1000, output_tokens: int = 250) -> dict[str, object]:
    return {
        GenAI.SYSTEM: "google",
        GenAI.REQUEST_MODEL: model,
        GenAI.RESPONSE_MODEL: model,
        GenAI.USAGE_INPUT_TOKENS: input_tokens,
        GenAI.USAGE_OUTPUT_TOKENS: output_tokens,
    }


def test_enrich_gemini_3_flash_no_catalog_miss() -> None:
    res = enrich_cost(_span("gemini-3-flash"), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enrich_gemini_2_5_pro_no_catalog_miss() -> None:
    res = enrich_cost(_span("gemini-2.5-pro"), seed_catalog(), at=AT)
    assert res.catalog_miss is False
    assert res.server_cost_micro_usd is not None
    assert res.server_cost_micro_usd > 0


def test_enriched_gemini_span_maps_to_row_with_cost() -> None:
    # End-to-end through the mapping: enriched google span -> otel row with a non-zero
    # EstimatedCost and GenAiSystem="google".
    res = enrich_cost(_span("gemini-3-flash", 1_000_000, 1_000_000), seed_catalog(), at=AT)
    row = span_to_row(res.attributes, tenant_id="tn-1", effective_ts_ns=1_700_000_000_000_000_000)
    from gateway.mapping import COLUMNS

    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["GenAiSystem"] == "google"
    assert by_col["GenAiRequestModel"] == "gemini-3-flash"
    assert by_col["EstimatedCost"] > 0
