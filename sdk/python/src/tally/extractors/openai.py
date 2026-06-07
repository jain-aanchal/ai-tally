# SPDX-License-Identifier: Apache-2.0
"""OpenAI Chat Completions extractor — version 1 (``openai_v1``).

Implements CTO-41.

Maps an OpenAI Chat Completions *response* ``usage`` object into the internal ``gen_ai.*`` attribute
dict defined by :mod:`tally.schema`:

* ``usage.prompt_tokens``                       -> ``gen_ai.usage.input_tokens``
* ``usage.completion_tokens``                   -> ``gen_ai.usage.output_tokens``
* ``usage.prompt_tokens_details.cached_tokens`` -> ``gen_ai.usage.cached_input_tokens``

Tool-call accounting: each tool/function call present in ``choices[].message.tool_calls`` is emitted
as its own attribute dict carrying ``gen_ai.tool.name`` / ``gen_ai.tool.call_id`` and the
``gen_ai.operation.name == "tool"``, alongside the primary ``chat`` dict. This represents the
cost/usage shape of tool-calling turns within the existing schema — the schema has a home for a tool
name and id, but *not* for a raw tool-call count, so a count is intentionally omitted rather than
fabricated as a new key (noted in the PR body).

Defensive by construction: missing/null ``usage``, missing fields, and unexpected types never raise
— the extractor returns whatever subset it can salvage. No message content or secrets are ever read,
only usage/metadata.
"""

from __future__ import annotations

from tally.schema import GenAI

_SYSTEM = "openai"


def _get(obj: object, key: str, default: object = None) -> object:
    """Attribute-or-key accessor (supports SDK objects and plain dicts); never raises."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_int(value: object) -> int | None:
    """Coerce to a non-negative int, or ``None`` if not a sane integer. ``bool`` is rejected."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_str(value: object) -> str | None:
    """Return ``value`` if it is a non-empty str, else ``None``."""
    return value if isinstance(value, str) and value else None


class OpenAIExtractorV1:
    """Extractor for OpenAI Chat Completions responses (response shape v1)."""

    key = "openai_v1"

    def extract(self, response: object) -> list[dict[str, object]]:
        """Return ``[chat_attrs, *tool_attrs]``. Never raises on malformed input."""
        attrs: list[dict[str, object]] = [self._chat_attrs(response)]
        attrs.extend(self._tool_attrs(response))
        return attrs

    def _chat_attrs(self, response: object) -> dict[str, object]:
        """Primary ``chat`` attribute dict: system, model, and token usage."""
        out: dict[str, object] = {GenAI.SYSTEM: _SYSTEM, GenAI.OPERATION_NAME: "chat"}

        model = _as_str(_get(response, "model"))
        if model is not None:
            out[GenAI.RESPONSE_MODEL] = model

        usage = _get(response, "usage")
        prompt = _as_int(_get(usage, "prompt_tokens"))
        if prompt is not None:
            out[GenAI.USAGE_INPUT_TOKENS] = prompt
        completion = _as_int(_get(usage, "completion_tokens"))
        if completion is not None:
            out[GenAI.USAGE_OUTPUT_TOKENS] = completion

        details = _get(usage, "prompt_tokens_details")
        cached = _as_int(_get(details, "cached_tokens"))
        if cached is not None:
            out[GenAI.USAGE_CACHED_INPUT_TOKENS] = cached

        return out

    def _tool_attrs(self, response: object) -> list[dict[str, object]]:
        """One attribute dict per tool call across all choices; empty when there are none."""
        out: list[dict[str, object]] = []
        choices = _get(response, "choices")
        if not isinstance(choices, (list, tuple)):
            return out
        for choice in choices:
            message = _get(choice, "message")
            tool_calls = _get(message, "tool_calls")
            if not isinstance(tool_calls, (list, tuple)):
                continue
            for call in tool_calls:
                attrs: dict[str, object] = {
                    GenAI.SYSTEM: _SYSTEM,
                    GenAI.OPERATION_NAME: "tool",
                }
                call_id = _as_str(_get(call, "id"))
                if call_id is not None:
                    attrs[GenAI.TOOL_CALL_ID] = call_id
                name = _as_str(_get(_get(call, "function"), "name"))
                if name is not None:
                    attrs[GenAI.TOOL_NAME] = name
                out.append(attrs)
        return out


# Register on import (the package __init__ imports this module for its side effect).
from tally.extractors import register  # noqa: E402

register(OpenAIExtractorV1())
