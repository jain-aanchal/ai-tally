# SPDX-License-Identifier: Apache-2.0
"""End-to-end integration: start_trace -> record_llm_call -> conformant span with cost."""

from __future__ import annotations

from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace, with_trace_context
from tally.egress import BatchProcessor, MemoryTransport
from tally.guardrails import CostLimitExceededException, GuardrailConfig, GuardrailState, Mode
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig, TraceSignals
from tally.schema import GenAI, validate_span_attributes


def _client(**kw):
    return TallyClient(catalog=seed_catalog(), **kw)


def test_end_to_end_emits_conformant_span_with_cost():
    exporter = MemoryExporter()
    client = _client(exporter=exporter, sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with with_trace_context(trace_id="t1", feature_tag="research", session_id="s1"):
        result = client.record_llm_call(
            provider="openai",
            model="gpt-5-mini",
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
    assert result.kept is True
    assert result.cost_micro_usd == 2_250_000
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.FEATURE_TAG] == "research"
    assert span[GenAI.COST_ESTIMATED_MICRO_USD] == 2_250_000


def test_billing_counts_at_head_even_when_dropped():
    # body_rate 0 → analytics drops everything, billing still counts
    client = _client(sampler=Sampler(SamplingConfig(body_rate=0.0)))
    with start_trace(feature_tag="f"):
        r = client.record_llm_call(
            provider="openai", model="gpt-5-mini", usage=Usage(100, 50)
        )
    assert r.kept is False
    assert client.billing.trace_count == 1


def test_tail_kept_body_dropped():
    exporter = MemoryExporter()
    client = _client(exporter=exporter, sampler=Sampler(SamplingConfig(body_rate=0.0)))
    with with_trace_context(trace_id="agent-1"):
        r = client.record_llm_call(
            provider="openai", model="gpt-5", usage=Usage(10, 10),
            signals=TraceSignals(is_agent=True),  # tail → kept
        )
    assert r.kept is True
    assert len(exporter.spans) == 1


def test_records_via_processor_egress():
    transport = MemoryTransport()
    proc = BatchProcessor(transport, max_batch_size=10)
    client = _client(processor=proc, sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with start_trace():
        client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    assert proc.pending() == 1
    proc.flush_once()
    assert len(transport.delivered) == 1


def test_no_active_trace_notes_drop_but_does_not_raise():
    client = _client(sampler=Sampler(SamplingConfig(body_rate=1.0)))
    r = client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    assert r.trace_id is None
    assert client.observability.context_drop_count == 1


def test_record_never_raises_on_bad_catalog():
    # a catalog whose lookup explodes must not break record_llm_call
    class BoomCatalog:
        def lookup(self, *a, **k):
            raise RuntimeError("catalog down")

    client = TallyClient(catalog=BoomCatalog(), sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with start_trace():
        r = client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    # boundary returns a benign result; error counted
    assert client.observability.internal_error_count == 1
    assert r.kept is False


def test_guard_raises_in_graceful_mode():
    client = _client()
    state = GuardrailState(trace_id="t", cumulative_cost_micro_usd=5000)
    cfg = GuardrailConfig(mode=Mode.GRACEFUL, max_cost_micro_usd=1000)
    try:
        client.guard(state, cfg)
        raise AssertionError("expected CostLimitExceededException")
    except CostLimitExceededException:
        pass


def test_guard_observe_mode_proceeds():
    client = _client()
    state = GuardrailState(trace_id="t", step_count=99)
    v = client.guard(state, GuardrailConfig(mode=Mode.OBSERVE, max_steps=10))
    assert v.proceed is True and v.would_fire is True
