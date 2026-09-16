# SPDX-License-Identifier: Apache-2.0
"""A trace id this SDK mints for a trace-less span is marked as minted (CTO-401).

CTO-396 made every span carry a ``(trace_id, span_id)`` pair, which a span emitted outside a
``start_trace`` gets by having one invented for it. The gateway head meter counts distinct trace
ids, and a random id per span is indistinguishable from a real trace by construction, so that
traffic went from contributing zero billable traces to contributing one per span. The id has to
stay (it is the span's row identity in ClickHouse), so the fix is to say where it came from.
"""

from __future__ import annotations

from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import SPAN_ID_KEY, TRACE_ID_KEY, TRACE_ID_SYNTHETIC_KEY


def _client() -> tuple[TallyClient, MemoryExporter]:
    exporter = MemoryExporter()
    client = TallyClient(
        catalog=seed_catalog(),
        exporter=exporter,
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    return client, exporter


def test_trace_less_span_gets_a_marked_trace_id() -> None:
    client, exporter = _client()
    client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    (span,) = exporter.spans
    # The id is still there (CTO-396): the span keeps its own row identity.
    assert span[TRACE_ID_KEY]
    assert span[SPAN_ID_KEY]
    assert span[TRACE_ID_SYNTHETIC_KEY] is True


def test_trace_less_spans_still_get_distinct_ids() -> None:
    client, exporter = _client()
    for _ in range(50):
        client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    assert len({s[TRACE_ID_KEY] for s in exporter.spans}) == 50


def test_span_in_a_real_trace_is_not_marked() -> None:
    client, exporter = _client()
    with start_trace(feature_tag="checkout") as ctx:
        client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    (span,) = exporter.spans
    assert span[TRACE_ID_KEY] == ctx.trace_id
    # Absent, not False: the marker only ever appears on an id this SDK invented.
    assert TRACE_ID_SYNTHETIC_KEY not in span


def test_caller_supplied_ids_are_left_alone_and_unmarked() -> None:
    client, exporter = _client()
    client.ingest_span(
        {
            TRACE_ID_KEY: "tr-caller",
            SPAN_ID_KEY: "sp-caller",
            "gen_ai.system": "openai",
            "gen_ai.operation.name": "chat",
        }
    )
    (span,) = exporter.spans
    assert span[TRACE_ID_KEY] == "tr-caller"
    assert TRACE_ID_SYNTHETIC_KEY not in span
