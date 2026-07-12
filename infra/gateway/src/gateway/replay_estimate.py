"""Body-driven what-if estimate helpers (CTO-128).

`/estimate` lets an operator ask "what would last week's workload cost if we swapped to a
cheaper candidate model *and* tightened the system prompt?" The candidate-model swap was
already covered by ``/v1/replay`` (CTO-113). The only new behavior here is applying a
``system_prompt_override`` to the captured envelope *before* the candidate call, so the
projected cost reflects the new prompt length rather than the captured one.

We do not have a real tokenizer wired in the gateway; the rest of the codebase estimates at
4 chars/token (see ``app._estimate_judge_cost``), so we stay consistent with that. Applying
the override:

1. Reads the captured system prompt from the envelope (``system_prompt`` / ``system``).
2. Computes the token delta between the captured prompt and the override at 4 chars/token.
3. Rewrites the envelope's ``input_tokens`` by that delta (floored at 1) and swaps the
   system-prompt field to the override text.

The mock candidate client reads ``input_tokens``/``output_tokens`` straight off the envelope,
so the rewritten count flows into the executor's pricing unchanged — no new mock path. A real
provider client would re-tokenize the actual rewritten prompt; the 4-chars/token estimate is
the honest v1 approximation and is documented as such in the projection diagnostics.
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4

# Envelope keys that may carry the captured system prompt, in precedence order.
_SYSTEM_PROMPT_KEYS = ("system_prompt", "system")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate at 4 chars/token — matches the rest of the gateway's heuristics."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def _captured_system_prompt(envelope: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(key, text)`` for the captured system prompt, or ``(None, "")`` if absent."""
    for key in _SYSTEM_PROMPT_KEYS:
        v = envelope.get(key)
        if isinstance(v, str):
            return key, v
    return None, ""


def apply_system_prompt_override(
    envelope: dict[str, Any], override: str | None
) -> dict[str, Any]:
    """Return a copy of ``envelope`` with the system prompt swapped to ``override``.

    The envelope's ``input_tokens`` is adjusted by the (override - captured) token delta so the
    projected cost reflects the new prompt length. When ``override`` is ``None`` or empty, the
    envelope is returned unchanged (just shallow-copied so callers never mutate the original).
    """
    out = dict(envelope)
    if not override:
        return out

    captured_key, captured_text = _captured_system_prompt(envelope)
    delta = _estimate_tokens(override) - _estimate_tokens(captured_text)

    try:
        base_input = int(envelope.get("input_tokens") or 0)
    except (TypeError, ValueError):
        base_input = 0
    out["input_tokens"] = max(1, base_input + delta)

    # Swap the system-prompt field to the override; default to `system_prompt` when the captured
    # envelope had no system prompt at all (so the override is still represented).
    out[captured_key or "system_prompt"] = override
    return out
