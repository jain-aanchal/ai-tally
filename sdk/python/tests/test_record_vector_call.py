# SPDX-License-Identifier: Apache-2.0
"""CTO-142 — record_vector_call lands spans in the Vector cost-layer bucket."""

from __future__ import annotations

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.schema import GenAI, validate_span_attributes


def _client(exporter: MemoryExporter | None = None) -> TallyClient:
    return TallyClient(exporter=exporter or MemoryExporter())


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


def test_default_pricing_lookup_when_cost_omitted():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_vector_call(provider="pinecone", index="docs", operation="query")
    client.record_vector_call(provider="pinecone", index="docs", operation="upsert")
    client.record_vector_call(provider="weaviate", index="docs", operation="query")
    client.record_vector_call(provider="qdrant", index="docs", operation="query")

    costs = [s[GenAI.TOOL_COST_MICRO_USD] for s in exporter.spans]
    assert costs == [400, 200, 300, 250]


def test_unknown_pair_defaults_to_zero():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_vector_call(provider="acme", index="docs", operation="frobnicate")

    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 0
    assert span[GenAI.OPERATION_NAME] == "vector"


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
