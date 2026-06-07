# SPDX-License-Identifier: Apache-2.0
"""CTO-48 — OpenAI auto-instrumentation, tested with a fake client (no network)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tally.context import with_trace_context
from tally.instrumentation import instrument_openai_create
from tally.instrumentation.openai import OpenAIInstrumentor
from tally.pricing import seed_catalog
from tally.schema import GenAI, validate_span_attributes


# --- fake OpenAI response objects ---
@dataclass
class _Details:
    cached_tokens: int = 0


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _Details | None = None


@dataclass
class _Resp:
    model: str
    usage: _Usage


def _fake_create(*, model, cached=0, prompt=1000, completion=500, **_):
    return _Resp(model=model, usage=_Usage(prompt, completion, _Details(cached)))


def test_extract_usage_object_and_dict():
    inst = OpenAIInstrumentor()
    obj = _Resp("gpt-5-mini", _Usage(10, 20, _Details(4)))
    u = inst.extract_usage(obj)
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens) == (10, 20, 4)

    as_dict = {"model": "gpt-5-mini", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    u2 = inst.extract_usage(as_dict)
    assert (u2.input_tokens, u2.output_tokens) == (7, 3)


def test_wrap_emits_conformant_span_with_cost():
    spans: list[dict] = []
    wrapped = instrument_openai_create(
        _fake_create, on_span=spans.append, catalog=seed_catalog()
    )
    resp = wrapped(model="gpt-5-mini", prompt=1_000_000, completion=1_000_000)
    assert isinstance(resp, _Resp)  # original response returned unchanged
    assert len(spans) == 1
    attrs = spans[0]
    assert validate_span_attributes(attrs) == []
    assert attrs[GenAI.SYSTEM] == "openai"
    assert attrs[GenAI.USAGE_INPUT_TOKENS] == 1_000_000
    # 0.25 + 2.00 USD = 2_250_000 micro
    assert attrs[GenAI.COST_ESTIMATED_MICRO_USD] == 2_250_000


def test_feature_tag_from_context():
    spans: list[dict] = []
    wrapped = instrument_openai_create(_fake_create, on_span=spans.append, catalog=seed_catalog())
    with with_trace_context(trace_id="t1", feature_tag="research", session_id="s1"):
        wrapped(model="gpt-5-mini")
    assert spans[0][GenAI.FEATURE_TAG] == "research"
    assert spans[0][GenAI.SESSION_ID] == "s1"


def test_provider_errors_propagate():
    def boom(**_):
        raise RuntimeError("openai 500")

    wrapped = instrument_openai_create(boom, on_span=lambda a: None)
    with pytest.raises(RuntimeError, match="openai 500"):
        wrapped(model="gpt-5-mini")


def test_faulty_on_span_never_breaks_call():
    def bad_sink(_attrs):
        raise ValueError("sink boom")

    wrapped = instrument_openai_create(_fake_create, on_span=bad_sink, catalog=seed_catalog())
    # instrumentation must not break the provider call
    resp = wrapped(model="gpt-5-mini")
    assert isinstance(resp, _Resp)


def test_no_catalog_means_no_cost():
    spans: list[dict] = []
    wrapped = instrument_openai_create(_fake_create, on_span=spans.append)  # no catalog
    wrapped(model="gpt-5-mini")
    assert GenAI.COST_ESTIMATED_MICRO_USD not in spans[0]


def test_cached_tokens_priced_cheaper():
    spans: list[dict] = []
    wrapped = instrument_openai_create(_fake_create, on_span=spans.append, catalog=seed_catalog())
    wrapped(model="gpt-5-mini", prompt=1_000_000, completion=0, cached=1_000_000)
    # all input cached → 0.025 USD = 25_000 micro
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] == 25_000
