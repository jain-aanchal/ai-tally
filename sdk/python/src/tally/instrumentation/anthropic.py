# SPDX-License-Identifier: Apache-2.0
"""Anthropic instrumentor - Messages API, sync/async/streaming (CTO-260 §4.1/§4.2/§4.3).

Mirrors :mod:`tally.instrumentation.openai`. A pure function over a provider response object,
testable with fakes and no network. Reads only ``usage`` / ``model`` metadata:

* ``usage.input_tokens`` + ``cache_creation_input_tokens`` + ``cache_read_input_tokens``
                                    -> ``gen_ai.usage.input_tokens``
* ``usage.output_tokens``           -> ``gen_ai.usage.output_tokens``
* ``usage.cache_read_input_tokens`` -> ``gen_ai.usage.cached_input_tokens`` (where present)

Anthropic's ``input_tokens`` is NOT a total: it excludes both cache buckets. See
``docs/anthropic-cache-tokens.md`` and :func:`_prompt_tokens` (CTO-374).

Streaming: Anthropic reports input tokens on the ``message_start`` event and the running output
count on ``message_delta`` events, so :meth:`accumulate` folds both and :meth:`finalize` returns
the terminal totals. A stream that never yields a usage-bearing event finalizes to ``None`` usage
(honest null tokens, never a fabricated zero).
"""

from __future__ import annotations

from collections.abc import Callable

from tally.instrumentation.base import ProviderInstrumentor, wrap_create
from tally.pricing import PriceCatalog, Usage
from tally.safety import SelfObservability


def _get(obj: object, key: str, default: object = None) -> object:
    """Attribute-or-key accessor (supports SDK objects and dicts)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _prompt_tokens(usage: object) -> int:
    """Total prompt tokens for an Anthropic response (CTO-374).

    Anthropic bills a prompt in three separately reported pieces, and ``input_tokens`` is only the
    first of them: it EXCLUDES ``cache_creation_input_tokens`` and ``cache_read_input_tokens``
    rather than totalling them. Reading it as the whole prompt made a call that served 18923 tokens
    from cache and 21 fresh ones record 21.

    Worse, it did not surface as a missing number. ``Usage.cached_input_tokens`` is defined
    repo-wide as a SUBSET of ``input_tokens`` (the edge proxy's ``CachedInputTokens``, and the
    ``min(cached, input)`` in :func:`tally.pricing.compute_cost_micro_usd`), so passing a bucket
    that is disjoint from ``input_tokens`` into it meant the clamp silently priced the entire
    18944-token prompt as 21 cached tokens.

    This mirrors ``anthropicPrompt`` in ``infra/edge-proxy/internal/proxy/provider.go`` so the two
    implementations agree on the same response. Folding the cache-write bucket into the total is a
    documented approximation, not a guess: the token count is then right, and the write share
    prices at the standard input rate instead of the write premium because there is no
    ``PriceType.CACHE_WRITE`` to bill it at. ``docs/anthropic-cache-tokens.md`` records why that
    trade is the better of the two available errors, and what closing it properly requires.
    """
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        total += int(_get(usage, key, 0) or 0)
    return total


# The three separately billed pieces of an Anthropic prompt, in the order the API reports them.
# input_tokens is only the first: see _prompt_tokens.
_PROMPT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _fold_usage(state: dict, usage: object) -> None:
    """Fold one streamed usage block into the accumulator, last-wins per field (CTO-374).

    Mirrors ``foldAnthropicUsage`` in ``infra/edge-proxy/internal/proxy/stream.go``. The buckets are
    kept apart in the state and summed once in :meth:`AnthropicInstrumentor.finalize`, because the
    cache buckets arrive on either message_start or message_delta, so totalling at the first event
    that carries a prompt would drop whatever the second one reports.

    A field the provider did not mention is left absent rather than written as 0, so an unreported
    bucket stays distinguishable from a reported zero for as long as the state holds it.
    """
    if usage is None:
        return
    for key in _PROMPT_KEYS:
        value = _get(usage, key)
        if value is not None:
            state[key] = int(value or 0)
    # message_start reports the output tokens generated so far (typically 1) and message_delta the
    # cumulative total, so last-wins lands on the final count. A stream cut off after message_start
    # keeps the partial count the provider actually reported rather than discarding it.
    output = _get(usage, "output_tokens")
    if output is not None:
        state["output_tokens"] = int(output or 0)


def _usage_from(usage: object) -> Usage | None:
    if usage is None:
        return None
    output_tokens = int(_get(usage, "output_tokens", 0) or 0)
    cached = int(_get(usage, "cache_read_input_tokens", 0) or 0)
    return Usage(
        input_tokens=_prompt_tokens(usage),
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )


class AnthropicInstrumentor:
    """Anthropic Messages API (``client.messages.create`` and ``client.messages.stream``)."""

    system = "anthropic"
    operation = "chat"

    def request_model(self, args: tuple, kwargs: dict) -> str | None:
        return kwargs.get("model")

    def response_model(self, response: object) -> str | None:
        model = _get(response, "model")
        return model if isinstance(model, str) else None

    def extract_usage(self, response: object) -> Usage | None:
        return _usage_from(_get(response, "usage"))

    # --- streaming (CTO-260 §4.3) ---
    def accumulate(self, state: dict, chunk: object) -> None:
        # message_start carries the model and the prompt buckets on a nested message object.
        message = _get(chunk, "message")
        if message is not None:
            model = _get(message, "model")
            if isinstance(model, str) and model:
                state["model"] = model
            _fold_usage(state, _get(message, "usage"))
        # message_delta carries the cumulative output count, and may carry cache buckets too.
        if _get(chunk, "usage") is not None:
            _fold_usage(state, _get(chunk, "usage"))
            model = _get(chunk, "model")
            if isinstance(model, str) and model:
                state.setdefault("model", model)

    def finalize(self, state: dict) -> tuple[str | None, Usage | None]:
        # CTO-374: a stream whose only usage event reported cache buckets (a fully cached prompt
        # reports input_tokens alongside them, but a fold that saw only a cache field must still
        # count as having seen usage) has real tokens to report. Keying this on input_tokens alone
        # would finalize it to None and lose the whole prompt.
        if not any(k in state for k in (*_PROMPT_KEYS, "output_tokens")):
            return state.get("model"), None
        usage = Usage(
            input_tokens=sum(int(state.get(k, 0)) for k in _PROMPT_KEYS),
            output_tokens=int(state.get("output_tokens", 0)),
            cached_input_tokens=int(state.get("cache_read_input_tokens", 0)),
        )
        return state.get("model"), usage


# satisfy the Protocol at import time (structural; a no-op assertion for readers)
_INSTRUMENTOR: ProviderInstrumentor = AnthropicInstrumentor()


def instrument_anthropic_create(
    create_fn: Callable[..., object],
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
) -> Callable[..., object]:
    """Wrap ``client.messages.create`` so each call emits a conformant span (sync)."""
    return wrap_create(
        create_fn,
        AnthropicInstrumentor(),
        on_span=on_span,
        obs=obs,
        catalog=catalog,
        tenant_id=tenant_id,
    )
