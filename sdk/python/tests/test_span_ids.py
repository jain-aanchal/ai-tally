# SPDX-License-Identifier: Apache-2.0
"""Every emitted span carries its own ``(trace_id, span_id)`` (CTO-396).

Before this, ``build_span_attributes`` emitted only ``gen_ai.*`` keys, so a batch of SDK spans all
keyed to ``(None, None)`` and the gateway's intra-batch dedup discarded all but the first.
"""

from __future__ import annotations

from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace
from tally.egress import BatchProcessor, MemoryTransport
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import (
    SPAN_ID_KEY,
    TRACE_ID_KEY,
    SpanFields,
    validate_span_attributes,
)


def _client(**kw) -> TallyClient:
    return TallyClient(
        catalog=seed_catalog(), sampler=Sampler(SamplingConfig(body_rate=1.0)), **kw
    )


def _record(client: TallyClient, n: int) -> None:
    for _ in range(n):
        client.record_llm_call(
            provider="openai", model="gpt-5-mini", usage=Usage(10, 5)
        )


def test_every_span_gets_a_distinct_span_id() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace(feature_tag="f"):
        _record(client, 50)

    span_ids = [s[SPAN_ID_KEY] for s in exporter.spans]
    assert len(span_ids) == 50
    assert len(set(span_ids)) == 50


def test_spans_in_one_trace_share_the_trace_id() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace(feature_tag="f") as ctx:
        _record(client, 3)

    assert {s[TRACE_ID_KEY] for s in exporter.spans} == {ctx.trace_id}


def test_separate_traces_do_not_share_a_trace_id() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    for _ in range(2):
        with start_trace(feature_tag="f"):
            _record(client, 1)

    assert len({s[TRACE_ID_KEY] for s in exporter.spans}) == 2


def test_span_without_an_active_trace_still_gets_unique_ids() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    _record(client, 3)  # no start_trace: the trace-drop path

    assert len({s[SPAN_ID_KEY] for s in exporter.spans}) == 3
    # A trace-less span gets a trace id of its own rather than sharing one with every other
    # trace-less span, which would collapse them all into one apparent trace.
    assert len({s[TRACE_ID_KEY] for s in exporter.spans}) == 3


def test_ids_are_lowercase_hex_in_the_proxy_shape() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace():
        _record(client, 1)

    span = exporter.spans[0]
    assert len(span[SPAN_ID_KEY]) == 16  # 8 bytes, like the edge proxy's randomHex(8)
    assert len(span[TRACE_ID_KEY]) == 32  # 16 bytes
    for key in (SPAN_ID_KEY, TRACE_ID_KEY):
        assert all(c in "0123456789abcdef" for c in span[key])


def test_caller_supplied_ids_are_preserved() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    client.ingest_span({TRACE_ID_KEY: "tr-1", SPAN_ID_KEY: "sp-1"})
    client.ingest_span({"TraceId": "tr-2", "SpanId": "sp-2"})

    assert exporter.spans[0][SPAN_ID_KEY] == "sp-1"
    assert exporter.spans[1] == {"TraceId": "tr-2", "SpanId": "sp-2"}


def test_ids_also_reach_a_processor_egress_path() -> None:
    transport = MemoryTransport()
    proc = BatchProcessor(transport, max_batch_size=10)
    client = _client(processor=proc)
    with start_trace():
        _record(client, 4)
    proc.flush_once()

    sent = transport.delivered
    assert len(sent) == 4
    assert len({s[SPAN_ID_KEY] for s in sent}) == 4


def test_all_span_kinds_carry_ids() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace():
        client.record_span(SpanFields(system="openai", operation="chat"))
        client.record_tool_call(provider="openai", tool="web_search", cost_micro_usd=10)
        client.record_embedding_call(
            provider="openai", model="text-embedding-3-small", input_tokens=100
        )
        client.record_vector_call(
            provider="pinecone", index="idx", operation="query", cost_micro_usd=5
        )

    assert len(exporter.spans) == 4
    assert all(s.get(SPAN_ID_KEY) and s.get(TRACE_ID_KEY) for s in exporter.spans)


def test_emitted_span_stays_schema_conformant() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace(feature_tag="f"):
        _record(client, 1)

    assert validate_span_attributes(exporter.spans[0]) == []


def test_result_attributes_are_not_mutated_by_emit() -> None:
    # The caller's own dict must not sprout keys behind its back; the ids are stamped on a copy.
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    caller_dict: dict[str, object] = {"gen_ai.system": "openai"}
    client.ingest_span(caller_dict)

    assert caller_dict == {"gen_ai.system": "openai"}
    assert exporter.spans[0][SPAN_ID_KEY]
