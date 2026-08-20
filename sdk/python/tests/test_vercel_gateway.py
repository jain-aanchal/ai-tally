# SPDX-License-Identifier: Apache-2.0
"""Vercel AI Gateway true-upstream attribution (CTO-161).

Proves the two things the ticket asks the SDK side to demonstrate:
  1. a captured Vercel-AI-Gateway response shape resolves to the TRUE upstream
     ``(provider, model, usage)`` so ``record_llm_call`` prices it from the catalog to the
     right non-zero cost — the gateway hop never launders the spend into a generic "vercel";
  2. an unresolvable upstream attributes to ``unknown`` with a null cost — never a guess.
"""

from __future__ import annotations

from datetime import date

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.pricing import Usage, compute_cost_micro_usd, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import GenAI, validate_span_attributes
from tally.vercel_gateway import (
    UNKNOWN,
    record_gateway_llm_call,
    resolve_upstream,
)

AT = date(2026, 6, 1)


# A captured Vercel AI Gateway chat-completion response (OpenAI-compatible shape). The gateway
# namespaces the model id by creator ("openai/gpt-4o-mini") and returns its own token counts,
# including a cached-input tier under prompt_tokens_details. Bodies elided (counts only).
_GATEWAY_OPENAI_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "model": "openai/gpt-4o-mini",
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 250,
        "total_tokens": 1250,
        "prompt_tokens_details": {"cached_tokens": 200},
    },
    "providerMetadata": {
        "gateway": {
            "provider": "openai",
            "routing": {"originalModelId": "openai/gpt-4o-mini"},
        }
    },
}

# The AI-SDK result shape: namespaced modelId + camelCase usage under a top-level usage block.
_GATEWAY_ANTHROPIC_AISDK = {
    "modelId": "anthropic/claude-sonnet-4-5",
    "usage": {"inputTokens": 2000, "outputTokens": 500, "cachedInputTokens": 100},
}


# --- resolution ---------------------------------------------------------------


def test_resolve_openai_from_namespaced_model_id() -> None:
    att = resolve_upstream(_GATEWAY_OPENAI_RESPONSE)
    assert att.resolved is True
    assert att.provider == "openai"
    assert att.model == "gpt-4o-mini"
    # Gateway's own usage numbers are preferred, including the cached tier.
    assert att.usage == Usage(input_tokens=1000, output_tokens=250, cached_input_tokens=200)


def test_resolve_anthropic_from_aisdk_shape() -> None:
    att = resolve_upstream(_GATEWAY_ANTHROPIC_AISDK)
    assert att.resolved is True
    assert att.provider == "anthropic"
    assert att.model == "claude-sonnet-4-5"
    assert att.usage == Usage(input_tokens=2000, output_tokens=500, cached_input_tokens=100)


def test_resolve_prefers_gateway_usage_over_fallback() -> None:
    att = resolve_upstream(
        _GATEWAY_OPENAI_RESPONSE,
        fallback_usage=Usage(9999, 9999, 9999),
    )
    # The gateway reported usage, so the locally-counted fallback is ignored.
    assert att.usage == Usage(1000, 250, 200)


def test_resolve_falls_back_to_token_count_when_gateway_omits_usage() -> None:
    md = {"model": "openai/gpt-4o-mini"}  # no usage block
    att = resolve_upstream(md, fallback_usage=Usage(500, 120, 0))
    assert att.resolved is True
    assert att.usage == Usage(500, 120, 0)


def test_amazon_creator_slug_maps_to_bedrock_provider() -> None:
    att = resolve_upstream({"model": "amazon/nova-pro", "usage": {"prompt_tokens": 10}})
    assert att.resolved is True
    assert att.provider == "bedrock"
    assert att.model == "nova-pro"


# --- catalog pricing (true upstream, not "vercel") ----------------------------


def test_captured_gateway_response_prices_from_catalog() -> None:
    att = resolve_upstream(_GATEWAY_OPENAI_RESPONSE)
    cat = seed_catalog()
    # gpt-4o-mini: 800 uncached input @ $0.15/MTok + 200 cached @ $0.075/MTok + 250 output
    # @ $0.60/MTok. Prove it resolves to the SAME cost as a direct openai/gpt-4o-mini call.
    via_gateway, v1 = compute_cost_micro_usd(cat, att.provider, att.model, att.usage, at=AT)
    direct, v2 = compute_cost_micro_usd(
        cat, "openai", "gpt-4o-mini", Usage(1000, 250, 200), at=AT
    )
    assert via_gateway == direct
    assert via_gateway is not None and via_gateway > 0
    assert v1 and v1 == v2


def test_record_gateway_llm_call_lands_true_upstream_span() -> None:
    exporter = MemoryExporter()
    client = TallyClient(
        exporter=exporter,
        catalog=seed_catalog(),
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    with with_trace_context(trace_id="trace-vercel-1"):
        result = record_gateway_llm_call(client, _GATEWAY_OPENAI_RESPONSE, at=AT)

    assert result.cost_micro_usd is not None and result.cost_micro_usd > 0
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    # True upstream on the span — NOT "vercel".
    assert span[GenAI.SYSTEM] == "openai"
    assert span[GenAI.REQUEST_MODEL] == "gpt-4o-mini"
    assert span[GenAI.USAGE_INPUT_TOKENS] == 1000
    assert span[GenAI.USAGE_OUTPUT_TOKENS] == 250
    assert validate_span_attributes(span) == []


# --- unknown-safe -------------------------------------------------------------


def test_unresolvable_upstream_attributes_to_unknown_null_cost() -> None:
    # Gateway returned usage but no model id we can name (e.g. a routing failure / redacted meta).
    md = {"usage": {"prompt_tokens": 1000, "completion_tokens": 250}}
    att = resolve_upstream(md)
    assert att.resolved is False
    assert att.provider == UNKNOWN
    assert att.model == UNKNOWN
    # Usage is still captured — we just can't price it.
    assert att.usage == Usage(1000, 250, 0)

    cost, version = compute_cost_micro_usd(
        seed_catalog(), att.provider, att.model, att.usage, at=AT
    )
    assert cost is None or cost == 0
    assert not version  # catalog miss -> dashboard renders "—", no fabricated number


def test_record_unknown_upstream_emits_span_with_no_cost() -> None:
    exporter = MemoryExporter()
    client = TallyClient(
        exporter=exporter,
        catalog=seed_catalog(),
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    with with_trace_context(trace_id="trace-vercel-unknown"):
        result = record_gateway_llm_call(
            client, {"usage": {"prompt_tokens": 42, "completion_tokens": 7}}, at=AT
        )
    # Recorded, never dropped — but priced null, attributed honestly to unknown.
    assert result.cost_micro_usd is None or result.cost_micro_usd == 0
    assert len(exporter.spans) == 1
    assert exporter.spans[0][GenAI.SYSTEM] == UNKNOWN
