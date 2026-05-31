"""CTO-41 — provider extractor framework, exercised against recorded OpenAI fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tally.extractors import (
    ProviderExtractor,
    available_extractors,
    get_extractor,
    register,
)
from tally.extractors.openai import OpenAIExtractorV1
from tally.schema import GenAI, validate_span_attributes

_FIXTURES = Path(__file__).parent / "fixtures" / "openai"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


# --- registry / pluggability -------------------------------------------------


def test_get_extractor_returns_versioned_singleton():
    ext = get_extractor("openai_v1")
    assert isinstance(ext, OpenAIExtractorV1)
    assert ext.key == "openai_v1"
    assert ext is get_extractor("openai_v1")


def test_openai_v1_is_listed():
    assert "openai_v1" in available_extractors()


def test_get_extractor_unknown_key_raises_keyerror():
    with pytest.raises(KeyError):
        get_extractor("does_not_exist_v9")


def test_satisfies_protocol():
    assert isinstance(OpenAIExtractorV1(), ProviderExtractor)


def test_register_rejects_empty_key_and_duplicates():
    class _NoKey:
        key = ""

        def extract(self, response):
            return []

    with pytest.raises(ValueError):
        register(_NoKey())

    class _Dup:
        key = "openai_v1"

        def extract(self, response):
            return []

    with pytest.raises(ValueError):
        register(_Dup())


def test_new_provider_registers_without_core_change():
    """Adding a provider is registration-only; dispatch needs no edit."""

    class _AcmeV1:
        key = "acme_test_v1"

        def extract(self, response):
            return [{GenAI.SYSTEM: "acme"}]

    register(_AcmeV1())
    try:
        assert get_extractor("acme_test_v1").extract(None) == [{GenAI.SYSTEM: "acme"}]
    finally:
        from tally.extractors import _REGISTRY

        _REGISTRY.pop("acme_test_v1", None)


# --- fixture-driven extraction -----------------------------------------------


def test_plain_completion_maps_usage():
    [chat] = get_extractor("openai_v1").extract(_load("chat_completion_plain.json"))
    assert chat[GenAI.SYSTEM] == "openai"
    assert chat[GenAI.OPERATION_NAME] == "chat"
    assert chat[GenAI.RESPONSE_MODEL] == "gpt-4o-mini-2024-07-18"
    assert chat[GenAI.USAGE_INPUT_TOKENS] == 24
    assert chat[GenAI.USAGE_OUTPUT_TOKENS] == 8
    assert chat[GenAI.USAGE_CACHED_INPUT_TOKENS] == 0
    assert validate_span_attributes(chat) == []


def test_cached_completion_maps_cached_input_tokens():
    [chat] = get_extractor("openai_v1").extract(_load("chat_completion_cached.json"))
    assert chat[GenAI.USAGE_INPUT_TOKENS] == 4096
    assert chat[GenAI.USAGE_CACHED_INPUT_TOKENS] == 3840
    assert validate_span_attributes(chat) == []


def test_tool_calls_produce_one_attr_dict_each():
    attrs = get_extractor("openai_v1").extract(_load("chat_completion_tool_calls.json"))
    chat = attrs[0]
    tools = attrs[1:]
    assert chat[GenAI.OPERATION_NAME] == "chat"
    assert chat[GenAI.USAGE_INPUT_TOKENS] == 88
    assert len(tools) == 2
    assert {t[GenAI.TOOL_NAME] for t in tools} == {"get_weather", "get_current_time"}
    assert {t[GenAI.TOOL_CALL_ID] for t in tools} == {"call_abc123weather", "call_def456time"}
    for t in tools:
        assert t[GenAI.OPERATION_NAME] == "tool"
        assert t[GenAI.SYSTEM] == "openai"
        assert validate_span_attributes(t) == []


def test_no_pii_or_secrets_in_attributes():
    """Message content / arguments must never leak into attributes."""
    attrs = get_extractor("openai_v1").extract(_load("chat_completion_tool_calls.json"))
    blob = json.dumps(attrs)
    assert "Paris" not in blob
    assert "arguments" not in blob
    assert "Europe/Paris" not in blob


# --- never crash on malformed input ------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"usage": None},
        {"usage": {}},
        {"usage": {"prompt_tokens": "lots", "completion_tokens": 1.5}},
        {"usage": {"prompt_tokens": True}},
        {"usage": {"prompt_tokens": -5}},
        {"usage": {"prompt_tokens_details": "nope"}},
        {"model": 123, "usage": {"prompt_tokens": 10}},
        {"choices": "not-a-list"},
        {"choices": [None]},
        {"choices": [{"message": {"tool_calls": "nope"}}]},
        {"choices": [{"message": {"tool_calls": [None, {}]}}]},
        "totally-wrong-type",
        42,
    ],
)
def test_never_raises_on_malformed_input(response):
    out = get_extractor("openai_v1").extract(response)
    assert isinstance(out, list)
    for attrs in out:
        # whatever subset survives must still be schema-conformant
        assert validate_span_attributes(attrs) == []


def test_malformed_usage_omits_bad_fields_but_keeps_system():
    ext = OpenAIExtractorV1()
    [chat] = ext.extract({"usage": {"prompt_tokens": "x", "completion_tokens": 9}})
    assert chat[GenAI.SYSTEM] == "openai"
    assert GenAI.USAGE_INPUT_TOKENS not in chat  # bad type dropped
    assert chat[GenAI.USAGE_OUTPUT_TOKENS] == 9  # good field kept
