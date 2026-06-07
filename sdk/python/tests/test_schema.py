# SPDX-License-Identifier: Apache-2.0
from decimal import Decimal

import pytest

from tally.schema import (
    DEFAULT_CURRENCY,
    GenAI,
    SpanFields,
    build_span_attributes,
    micro_to_usd,
    usd_to_micro,
    validate_span_attributes,
)


def test_build_emits_only_set_fields():
    attrs = build_span_attributes(SpanFields(system="openai", input_tokens=10))
    assert attrs[GenAI.SYSTEM] == "openai"
    assert attrs[GenAI.USAGE_INPUT_TOKENS] == 10
    assert GenAI.RESPONSE_MODEL not in attrs


def test_build_is_conformant():
    fields = SpanFields(
        system="openai",
        request_model="gpt-5-mini",
        response_model="gpt-5-mini",
        operation="chat",
        input_tokens=1200,
        output_tokens=300,
        cached_input_tokens=512,
        cost_estimated_micro_usd=1234,
        feature_tag="research_agent",
        session_id="sess_1",
        user_id_hash="a" * 64,
        user_id_hash_key_version="v1",
        agent_run_id="run_1",
        agent_step_index=3,
        tool_name="web_fetch",
        tool_cost_micro_usd=10,
    )
    attrs = build_span_attributes(fields)
    assert validate_span_attributes(attrs) == []
    # currency defaulted because a cost is present
    assert attrs[GenAI.COST_CURRENCY] == DEFAULT_CURRENCY


def test_unknown_key_is_violation():
    assert any("unknown" in v for v in validate_span_attributes({"llm.tokens": 5}))


def test_int_keys_reject_bool_and_float():
    assert validate_span_attributes({GenAI.USAGE_INPUT_TOKENS: True})
    assert validate_span_attributes({GenAI.USAGE_INPUT_TOKENS: 1.5})


def test_negative_tokens_rejected():
    assert validate_span_attributes({GenAI.USAGE_OUTPUT_TOKENS: -1})


def test_cost_requires_currency():
    violations = validate_span_attributes({GenAI.COST_ESTIMATED_MICRO_USD: 100})
    assert any("currency" in v for v in violations)


def test_operation_must_be_lowercase():
    assert validate_span_attributes({GenAI.OPERATION_NAME: "Chat"})


def test_bad_currency_rejected():
    assert validate_span_attributes(
        {GenAI.COST_ESTIMATED_MICRO_USD: 1, GenAI.COST_CURRENCY: "dollars"}
    )


@pytest.mark.parametrize(
    "usd,micro",
    [("0.0012", 1200), ("1", 1_000_000), ("0.0000005", 1), ("0.00000049", 0)],
)
def test_usd_micro_roundtrip(usd, micro):
    assert usd_to_micro(usd) == micro


def test_micro_to_usd():
    assert micro_to_usd(1200) == Decimal("0.00120000")
