# SPDX-License-Identifier: Apache-2.0
"""CTO-135 — record_tool_call lands spans in the Tools cost-layer bucket."""

from __future__ import annotations

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.schema import GenAI, validate_span_attributes


def _client(exporter: MemoryExporter | None = None) -> TallyClient:
    return TallyClient(exporter=exporter or MemoryExporter())


def test_explicit_cost_emits_tool_span():
    exporter = MemoryExporter()
    client = _client(exporter)
    with with_trace_context(trace_id="t1", feature_tag="research", session_id="s1"):
        client.record_tool_call(provider="tavily", tool="search", cost_micro_usd=10_000)

    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.OPERATION_NAME] == "tool"
    assert span[GenAI.TOOL_NAME] == "search"
    assert span[GenAI.TOOL_COST_MICRO_USD] == 10_000
    assert span[GenAI.SYSTEM] == "tavily"
    assert span[GenAI.FEATURE_TAG] == "research"
    assert span[GenAI.SESSION_ID] == "s1"


def test_default_pricing_lookup_when_cost_omitted():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_tool_call(provider="serpapi", tool="search")
    client.record_tool_call(provider="brave", tool="search")
    client.record_tool_call(provider="firecrawl", tool="scrape")

    costs = [s[GenAI.TOOL_COST_MICRO_USD] for s in exporter.spans]
    assert costs == [15_000, 5_000, 20_000]


def test_unknown_pair_defaults_to_zero():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_tool_call(provider="acme", tool="frobnicate")

    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 0
    assert span[GenAI.OPERATION_NAME] == "tool"


def test_call_id_and_tokens_ride_along():
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_tool_call(
        provider="tavily",
        tool="search",
        cost_micro_usd=10_000,
        input_tokens=12,
        output_tokens=34,
        call_id="call-7",
    )
    span = exporter.spans[0]
    assert span[GenAI.TOOL_CALL_ID] == "call-7"
    assert span[GenAI.USAGE_INPUT_TOKENS] == 12
    assert span[GenAI.USAGE_OUTPUT_TOKENS] == 34


def test_never_raises_when_exporter_throws():
    class BoomExporter:
        def export(self, attributes: dict[str, object]) -> None:
            raise RuntimeError("exporter down")

    client = TallyClient(exporter=BoomExporter())
    # Must not raise.
    client.record_tool_call(provider="tavily", tool="search", cost_micro_usd=10_000)
    assert client.observability.internal_error_count == 1
