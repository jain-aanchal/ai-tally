# SPDX-License-Identifier: Apache-2.0
"""Every emitted span carries its own ``(trace_id, span_id)`` (CTO-396).

Before this, ``build_span_attributes`` emitted only ``gen_ai.*`` keys, so a batch of SDK spans all
keyed to ``(None, None)`` and the gateway's intra-batch dedup discarded all but the first.
"""

from __future__ import annotations

import hashlib
import threading

from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace
from tally.egress import BatchProcessor, MemoryTransport
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import (
    SPAN_ID_KEY,
    TIMESTAMP_NS_KEY,
    TRACE_ID_KEY,
    SpanFields,
    validate_span_attributes,
)
from tally.timekeeping import assess
from tally.wire import BatchRequest


class _LockedExporter:
    """MemoryExporter with an explicit lock, so a concurrency test asserts about span identity
    rather than about whether ``list.append`` happens to be atomic."""

    def __init__(self) -> None:
        self.spans: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def export(self, attributes: dict[str, object]) -> None:
        with self._lock:
            self.spans.append(attributes)


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


def test_span_ids_are_unique_and_encode_nothing_about_the_call() -> None:
    """Replaces a test that asserted only length and character class (CTO-404).

    That test would have FAILED on a harmless change (a different id width) and PASSED on a harmful
    one: a counter and a hash of the model name are both 16 lowercase hex characters. What has to
    hold is that an id is unique and says nothing about the customer's call, so that is what is
    pinned. The proxy-compatible shape is still asserted, but it is no longer carrying the weight.
    """
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace(feature_tag="f"):
        _record(client, 200)  # 200 calls identical in every field

    span_ids = [s[SPAN_ID_KEY] for s in exporter.spans]
    # Identical input, 200 distinct ids: the id cannot be a function of the call.
    assert len(set(span_ids)) == 200

    # And specifically not a digest of the call's own fields, the harmful change the old test
    # would have accepted. No customer data may be recoverable from an id.
    for text in ("gpt-5-mini", "openai", "openai:gpt-5-mini", "chat"):
        raw = text.encode()
        for digest in (hashlib.sha256(raw).hexdigest(), hashlib.blake2b(raw).hexdigest()):
            assert digest[:16] not in span_ids

    # Shape, kept as an edge-proxy compatibility check (randomHex(8) / randomHex(16)).
    assert all(len(sid) == 16 for sid in span_ids)
    assert all(set(sid) <= set("0123456789abcdef") for sid in span_ids)
    assert all(len(s[TRACE_ID_KEY]) == 32 for s in exporter.spans)


def test_span_ids_are_not_a_predictable_sequence() -> None:
    """A counter is unique, lowercase hex and the right width, and would still be a regression.

    An id has to be unguessable, which is why the edge proxy uses raw randomness and why
    ``new_span_id`` does too. Any fixed stride fails here (CTO-404).
    """
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    _record(client, 200)

    values = sorted(int(s[SPAN_ID_KEY], 16) for s in exporter.spans)
    # strict=False on purpose: the two sequences differ in length by one, which is the point.
    strides = {b - a for a, b in zip(values, values[1:], strict=False)}
    assert len(strides) > 1  # a counter gives exactly one stride
    # 200 draws from 2**64 spread enormously; a counter spans less than 200.
    assert values[-1] - values[0] > 2**40


def test_id_generation_holds_under_concurrency() -> None:
    """Nothing tested the concurrency property, so a regression here would have been silent.

    16 threads times 500 calls: every span id distinct, and each thread's spans carry that thread's
    own trace id and no other's. contextvars are isolated per thread, which is what makes the second
    half true; a module-level id cache or a shared buffer would break one or the other (CTO-404).
    """
    exporter = _LockedExporter()
    client = _client(exporter=exporter)
    threads, per_thread = 16, 500
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()  # start together, to maximise overlap
        with start_trace(feature_tag="f"):
            _record(client, per_thread)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    spans = exporter.spans
    assert len(spans) == threads * per_thread
    assert len({s[SPAN_ID_KEY] for s in spans}) == threads * per_thread
    assert len({s[TRACE_ID_KEY] for s in spans}) == threads


def test_caller_supplied_ids_are_preserved() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    client.ingest_span({TRACE_ID_KEY: "tr-1", SPAN_ID_KEY: "sp-1"})
    client.ingest_span({"TraceId": "tr-2", "SpanId": "sp-2"})

    assert exporter.spans[0][SPAN_ID_KEY] == "sp-1"
    assert exporter.spans[0][TRACE_ID_KEY] == "tr-1"
    # Either spelling of the ids is left exactly as the caller set it. What CTO-404 adds is the one
    # field the caller did NOT supply: a span carrying no timestamp of its own inherits the
    # envelope's send time at the gateway, and that is different in every envelope, so the same
    # span re-sent would be stored twice.
    second = exporter.spans[1]
    assert second["TraceId"] == "tr-2"
    assert second["SpanId"] == "sp-2"
    assert set(second) == {"TraceId", "SpanId", TIMESTAMP_NS_KEY}
    assert isinstance(second[TIMESTAMP_NS_KEY], int)


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


# --- CTO-404: a span has to be storage-idempotent, and the result has to describe it -------------


def _effective_ts(span: dict, batch: BatchRequest, server_recv_ns: int) -> int:
    """The timestamp the row would be written under, mirroring ``gateway/app.py``.

    The gateway prefers the span's own ``timestamp_ns`` and falls back to the ENVELOPE's
    ``client_send_ts_ns``, then runs the skew clamp. Restated here because that fallback is the
    whole bug: it makes the stored row depend on which batch happened to carry the span.
    """
    client_ts = span.get(TIMESTAMP_NS_KEY)
    client_ts_ns = client_ts if isinstance(client_ts, int) else batch.client_send_ts_ns
    return assess(client_ts_ns, server_recv_ns).effective_ts_ns


def test_every_span_carries_its_own_metering_timestamp() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace():
        _record(client, 3)

    for span in exporter.spans:
        assert isinstance(span[TIMESTAMP_NS_KEY], int)
        assert span[TIMESTAMP_NS_KEY] > 0
    assert validate_span_attributes(exporter.spans[0]) == []  # still schema-conformant


def test_a_caller_supplied_timestamp_is_never_overwritten() -> None:
    # An OTel-shaped producer owns its own clock, exactly as it owns its own ids.
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    client.ingest_span({TIMESTAMP_NS_KEY: 1_700_000_000_000_000_000})
    client.ingest_span({"Timestamp": 1_700_000_000_000_000_000})  # the ClickHouse spelling

    assert exporter.spans[0][TIMESTAMP_NS_KEY] == 1_700_000_000_000_000_000
    assert TIMESTAMP_NS_KEY not in exporter.spans[1]  # the other spelling is left alone


def test_the_same_span_re_enveloped_keeps_one_storage_identity() -> None:
    """The reproduction (CTO-404).

    The same span, re-enveloped in a NEW BatchRequest after a restart, carried identical ids but a
    different time, because the time came from the envelope. Timestamp is part of the ClickHouse
    sorting key, so the ReplacingMergeTree saw two distinct rows and never collapsed them: the
    spend was counted twice, and no later query could tell which dollars were doubled.
    """
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace():
        _record(client, 1)
    span = exporter.spans[0]

    # Two envelopes built at different moments, delivered at different moments.
    batch_a = BatchRequest(tenant_id="t", sdk_version="0.0.1", resource_spans=[span])
    batch_b = BatchRequest(
        tenant_id="t",
        sdk_version="0.0.1",
        resource_spans=[span],
        client_send_ts_ns=batch_a.client_send_ts_ns + 90_000_000_000,
    )
    recv_a = batch_a.client_send_ts_ns + 1_000_000_000
    recv_b = batch_b.client_send_ts_ns + 1_000_000_000

    # Same ids AND same effective timestamp, so the two rows are one row.
    assert span[TRACE_ID_KEY] and span[SPAN_ID_KEY]
    assert _effective_ts(span, batch_a, recv_a) == _effective_ts(span, batch_b, recv_b)
    # And the clamp does not defeat it: a sane clock is used as-is, never rewritten to receive time.
    assert _effective_ts(span, batch_a, recv_a) == span[TIMESTAMP_NS_KEY]
    assert assess(span[TIMESTAMP_NS_KEY], recv_a).clamped is False

    # The envelope fallback, which is what a span without its own timestamp still gets, is exactly
    # what differed: kept as the contrast so the fix cannot be quietly undone.
    stripped = {k: v for k, v in span.items() if k != TIMESTAMP_NS_KEY}
    assert _effective_ts(stripped, batch_a, recv_a) != _effective_ts(stripped, batch_b, recv_b)


def test_two_identical_id_less_spans_are_both_stored_deliberately() -> None:
    """Pins the CTO-396 decision, which nothing tested (CTO-404).

    An id-less span is NOT a duplicate of the next id-less span: dedup needs a real identity and a
    missing id is not one. Every span this SDK emits now carries ids, so this is about a caller
    hand-building spans, and it is the rule the transport's item-id mirror has to match.
    """
    batch = BatchRequest(
        tenant_id="t",
        sdk_version="0.0.1",
        resource_spans=[{"gen_ai.system": "openai"}, {"gen_ai.system": "openai"}],
    )
    assert len(batch.deduplicated().resource_spans) == 2


def test_the_result_describes_the_span_that_was_emitted() -> None:
    """``LlmCallResult`` used to describe the pre-stamp copy, so it named neither id (CTO-404)."""
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    with start_trace(feature_tag="f") as ctx:
        result = client.record_llm_call(
            provider="openai", model="gpt-5-mini", usage=Usage(10, 5)
        )

    span = exporter.spans[0]
    assert result.trace_id == ctx.trace_id == span[TRACE_ID_KEY]
    assert result.attributes[SPAN_ID_KEY] == span[SPAN_ID_KEY]
    assert result.attributes[TIMESTAMP_NS_KEY] == span[TIMESTAMP_NS_KEY]


def test_a_trace_less_result_names_the_synthetic_trace_that_was_stored() -> None:
    """The half a customer actually hits: correlating ``result.trace_id`` with the dashboard.

    It used to be None while the stored span carried a real synthetic trace id, so the customer was
    told to go looking for nothing.
    """
    exporter = MemoryExporter()
    client = _client(exporter=exporter)
    result = client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))

    assert result.trace_id == exporter.spans[0][TRACE_ID_KEY]
    assert client.obs.synthetic_trace_count == 1
    assert client.obs.context_drop_count == 0  # nothing was dropped, so nothing says it was


def test_a_sampled_out_call_invents_no_span_identity() -> None:
    """Honest the other way: nothing was emitted, so there is no stored span to name."""
    exporter = MemoryExporter()
    client = TallyClient(
        catalog=seed_catalog(),
        sampler=Sampler(SamplingConfig(body_rate=0.0, mid_rate=0.0, tail_rate=0.0)),
        exporter=exporter,
    )
    result = client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))

    assert result.kept is False
    assert exporter.spans == []
    assert result.trace_id is None
    assert SPAN_ID_KEY not in result.attributes
