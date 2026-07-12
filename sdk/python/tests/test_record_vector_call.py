# SPDX-License-Identifier: Apache-2.0
"""CTO-142 / CTO-141 — record_vector_call lands spans in the Vector cost-layer bucket.

Default vector pricing now resolves from the versioned price catalog (CTO-141) under
``PriceType.VECTOR_CALL``, not the removed inline ``_VECTOR_PRICING`` dict.
"""

from __future__ import annotations

import logging

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.pricing import seed_catalog
from tally.schema import GenAI, validate_span_attributes


def _client(exporter: MemoryExporter | None = None) -> TallyClient:
    # Default pricing now resolves from the catalog (CTO-141), not an inline dict.
    return TallyClient(exporter=exporter or MemoryExporter(), catalog=seed_catalog())


def test_explicit_cost_emits_vector_span():
    exporter = MemoryExporter()
    client = _client(exporter)
    with with_trace_context(trace_id="t1", feature_tag="research", session_id="s1"):
        client.record_vector_call(
            provider="pinecone", index="docs", operation="query", cost_micro_usd=400
        )

    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.OPERATION_NAME] == "vector"
    assert span[GenAI.TOOL_NAME] == "pinecone.docs.query"
    assert span[GenAI.TOOL_COST_MICRO_USD] == 400
    assert span[GenAI.SYSTEM] == "pinecone"
    assert span[GenAI.FEATURE_TAG] == "research"
    assert span[GenAI.SESSION_ID] == "s1"
    # Explicit cost overrides the catalog → no version stamp.
    assert GenAI.COST_PRICE_CATALOG_VERSION not in span


def test_default_pricing_resolves_from_catalog_when_cost_omitted():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_vector_call(provider="pinecone", index="docs", operation="query")
    client.record_vector_call(provider="pinecone", index="docs", operation="upsert")
    client.record_vector_call(provider="weaviate", index="docs", operation="query")
    client.record_vector_call(provider="qdrant", index="docs", operation="query")

    costs = [s[GenAI.TOOL_COST_MICRO_USD] for s in exporter.spans]
    assert costs == [400, 200, 300, 250]
    for span in exporter.spans:
        assert span[GenAI.COST_PRICE_CATALOG_VERSION]


def test_explicit_cost_overrides_catalog():
    exporter = MemoryExporter()
    client = _client(exporter)
    # pinecone/query is seeded at 400; an explicit value must win.
    client.record_vector_call(
        provider="pinecone", index="docs", operation="query", cost_micro_usd=7
    )
    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 7
    assert GenAI.COST_PRICE_CATALOG_VERSION not in span


def test_unknown_pair_defaults_to_zero_and_warns(caplog):
    exporter = MemoryExporter()
    client = _client(exporter)
    with caplog.at_level(logging.WARNING, logger="tally"):
        client.record_vector_call(provider="acme", index="docs", operation="frobnicate")

    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 0
    assert span[GenAI.OPERATION_NAME] == "vector"
    assert any("no catalog vector price" in r.message for r in caplog.records)


def test_no_catalog_defaults_to_zero():
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter)
    client.record_vector_call(provider="pinecone", index="docs", operation="query")
    assert exporter.spans[0][GenAI.TOOL_COST_MICRO_USD] == 0


def test_real_seed_catalog_prices_pinecone_query():
    # Regression: the real seed_catalog() must price a known vector op > 0 (CTO-141).
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter, catalog=seed_catalog())
    client.record_vector_call(provider="pinecone", index="docs", operation="query")
    assert exporter.spans[0][GenAI.TOOL_COST_MICRO_USD] == 400


def test_never_raises_when_exporter_throws():
    class BoomExporter:
        def export(self, attributes: dict[str, object]) -> None:
            raise RuntimeError("exporter down")

    client = TallyClient(exporter=BoomExporter())
    # Must not raise.
    client.record_vector_call(
        provider="pinecone", index="docs", operation="query", cost_micro_usd=400
    )
    assert client.observability.internal_error_count == 1
