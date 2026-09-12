# SPDX-License-Identifier: Apache-2.0
"""CTO-374 - the SDK and the edge proxy must agree on Anthropic's three prompt buckets.

The proxy learned this in CTO-349 (`docs/anthropic-cache-tokens.md`); the SDK did not, and the
divergence was invisible because `min(cached, input)` in `compute_cost_micro_usd` clamped the
resulting nonsense into a plausible-looking small number instead of raising.

These tests read the PROXY's fixtures rather than defining their own, so the two implementations are
asserted against the same bytes. A fixture edited on one side now breaks the other side's test,
which is the point: the failure mode being guarded is the two paths quietly drifting apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tally.instrumentation.anthropic import AnthropicInstrumentor
from tally.pricing import Usage

_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "edge-proxy"
    / "internal"
    / "proxy"
    / "testdata"
    / "anthropic"
)


def _fixture(name: str) -> dict:
    path = _FIXTURES / name
    if not path.exists():  # pragma: no cover - a moved fixture should say so, not silently pass
        pytest.skip(f"proxy fixture not present at {path}")
    return json.loads(path.read_text())


def _usage_for(name: str) -> Usage | None:
    return AnthropicInstrumentor().extract_usage(_fixture(name))


def test_cache_read_folds_into_prompt_total():
    """The bug's headline case: 21 fresh + 0 writes + 18923 reads is an 18944-token prompt.

    The SDK used to report 21, and pricing then billed 21 CACHED tokens (min(18923, 21)), so a call
    costing roughly a cent was recorded at a fraction of a cent with no error anywhere.

    Mirrors TestAnthropicCacheTokensFoldIntoPromptTokens in provider_anthropic_test.go.
    """
    usage = _usage_for("message_cache_read.json")
    assert usage is not None
    assert usage.input_tokens == 18944
    assert usage.cached_input_tokens == 18923
    assert usage.output_tokens == 44


def test_cached_share_is_a_subset_of_the_total():
    """The invariant the whole repo's pricing math rests on.

    `compute_cost_micro_usd` bills (input - cached) at the standard rate and cached at the cached
    rate. That is only correct while cached <= input. Asserting it directly means a future edit that
    reintroduces the disjoint-bucket bug fails here, on the invariant, rather than in a variance
    report months later.
    """
    for name in (
        "message_cache_read.json",
        "message_end_turn.json",
        "message_tool_use.json",
    ):
        usage = _usage_for(name)
        assert usage is not None
        assert usage.cached_input_tokens <= usage.input_tokens, name


def test_uncached_response_totals_unchanged():
    """A response with no cache activity must be untouched by the fold.

    message_end_turn.json reports both buckets as an explicit 0, so the sum equals input_tokens and
    the fix is a no-op on the common case.
    """
    doc = _fixture("message_end_turn.json")
    usage = _usage_for("message_end_turn.json")
    assert usage is not None
    assert usage.input_tokens == doc["usage"]["input_tokens"]


def test_missing_cache_fields_are_not_invented():
    """message_tool_use.json omits the cache fields entirely.

    The prompt total is then input_tokens alone, and the cached share is 0, which `base.py` maps to
    a null `gen_ai.usage.cached_input_tokens` rather than asserting "caching saved nothing".
    """
    doc = _fixture("message_tool_use.json")
    assert "cache_read_input_tokens" not in doc["usage"]
    usage = _usage_for("message_tool_use.json")
    assert usage is not None
    assert usage.input_tokens == doc["usage"]["input_tokens"]
    assert usage.cached_input_tokens == 0


def test_streaming_folds_cache_buckets_from_message_delta():
    """The cache buckets arrive on message_start OR message_delta, so the fold spans events.

    This is why the accumulator keeps the three buckets apart and sums them in finalize: an earlier
    draft totalled at message_start and lost whatever message_delta reported. Mirrors
    foldAnthropicUsage in stream.go.
    """
    inst = AnthropicInstrumentor()
    state: dict = {}
    inst.accumulate(
        state,
        {
            "type": "message_start",
            "message": {"model": "claude-opus-5", "usage": {"input_tokens": 21}},
        },
    )
    inst.accumulate(
        state,
        {
            "type": "message_delta",
            "usage": {
                "output_tokens": 44,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 18923,
            },
        },
    )
    model, usage = inst.finalize(state)
    assert model == "claude-opus-5"
    assert usage is not None
    assert usage.input_tokens == 21 + 100 + 18923
    assert usage.cached_input_tokens == 18923
    assert usage.output_tokens == 44


def test_stream_with_no_usage_event_stays_unknown():
    """The honest-null half: no usage reported means no usage claimed, not a zero-token call."""
    model, usage = AnthropicInstrumentor().finalize({"model": "claude-opus-5"})
    assert model == "claude-opus-5"
    assert usage is None


def test_stream_reporting_only_a_cache_bucket_still_counts_as_usage():
    """A fold that saw a cache field but no input_tokens has real tokens to report.

    Keying the finalize guard on input_tokens alone would discard the entire prompt here.
    """
    inst = AnthropicInstrumentor()
    state: dict = {}
    inst.accumulate(state, {"type": "message_delta", "usage": {"cache_read_input_tokens": 512}})
    _, usage = inst.finalize(state)
    assert usage is not None
    assert usage.input_tokens == 512
    assert usage.cached_input_tokens == 512
