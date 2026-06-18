# SPDX-License-Identifier: Apache-2.0
"""Tests for context-window drop signalling (CTO-118).

These cover the *new* second mode of ``note_context_drop``: emitting span attributes
for messages trimmed before send. The legacy trace-drop path is covered in
``test_context.py``; here we verify it remains backwards-compatible.
"""

from __future__ import annotations

from tally.context import note_context_drop
from tally.safety import SelfObservability


def test_emits_three_attributes() -> None:
    """All three drop fields populate the expected span attribute keys."""
    obs = SelfObservability()
    attrs = note_context_drop(
        obs,
        dropped_messages=3,
        dropped_tokens=1200,
        window_used_pct=0.92,
    )
    assert attrs["gen_ai.context.dropped_messages"] == 3
    assert attrs["gen_ai.context.dropped_tokens"] == 1200
    assert attrs["gen_ai.context.window_used_pct"] == 0.92
    # Window-drop path must NOT bump the trace-drop counter — different semantic.
    assert obs.context_drop_count == 0


def test_mutates_caller_attrs() -> None:
    """When given an attrs dict, the function writes into it (so the caller can
    splice into a span-being-built without an extra merge step)."""
    obs = SelfObservability()
    existing: dict[str, object] = {"gen_ai.system": "openai"}
    out = note_context_drop(
        obs,
        dropped_messages=2,
        dropped_tokens=500,
        window_used_pct=0.5,
        attrs=existing,
    )
    assert out is existing
    assert existing["gen_ai.system"] == "openai"  # preserved
    assert existing["gen_ai.context.dropped_messages"] == 2


def test_backwards_compatible_no_args() -> None:
    """Legacy call site (no drop fields) still works — bumps the obs counter."""
    obs = SelfObservability()
    note_context_drop(obs, where="record_llm_call")
    assert obs.context_drop_count == 1
    assert "context drop" in obs.last_errors[-1]


def test_partial_call_one_field() -> None:
    """Caller may supply any subset of the three fields."""
    obs = SelfObservability()
    attrs = note_context_drop(obs, window_used_pct=0.81)
    assert attrs == {"gen_ai.context.window_used_pct": 0.81}
    assert obs.context_drop_count == 0  # not a trace drop


def test_negative_values_clamped_to_zero() -> None:
    """Don't trust the caller blindly — negatives become 0, not negatives."""
    obs = SelfObservability()
    attrs = note_context_drop(
        obs,
        dropped_messages=-5,
        dropped_tokens=-100,
        window_used_pct=-0.3,
    )
    assert attrs["gen_ai.context.dropped_messages"] == 0
    assert attrs["gen_ai.context.dropped_tokens"] == 0
    assert attrs["gen_ai.context.window_used_pct"] == 0.0


def test_pct_above_one_clamped() -> None:
    """A caller reporting >100% utilization is buggy; clamp rather than reject."""
    obs = SelfObservability()
    attrs = note_context_drop(obs, window_used_pct=1.4)
    assert attrs["gen_ai.context.window_used_pct"] == 1.0


def test_attributes_pass_schema_validation() -> None:
    """The emitted attribute dict must be conformant under validate_span_attributes."""
    from tally.schema import validate_span_attributes

    obs = SelfObservability()
    attrs = note_context_drop(
        obs,
        dropped_messages=4,
        dropped_tokens=2000,
        window_used_pct=0.75,
    )
    assert validate_span_attributes(attrs) == []
