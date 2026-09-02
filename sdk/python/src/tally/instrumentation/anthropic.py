# SPDX-License-Identifier: Apache-2.0
"""Anthropic instrumentor — Messages API, sync/async/streaming (CTO-260 §4.1/§4.2/§4.3).

Mirrors :mod:`tally.instrumentation.openai`. A pure function over a provider response object,
testable with fakes and no network. Reads only ``usage`` / ``model`` metadata:

* ``usage.input_tokens``            -> ``gen_ai.usage.input_tokens``
* ``usage.output_tokens``           -> ``gen_ai.usage.output_tokens``
* ``usage.cache_read_input_tokens`` -> ``gen_ai.usage.cached_input_tokens`` (where present)

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


def _usage_from(usage: object) -> Usage | None:
    if usage is None:
        return None
    input_tokens = int(_get(usage, "input_tokens", 0) or 0)
    output_tokens = int(_get(usage, "output_tokens", 0) or 0)
    cached = int(_get(usage, "cache_read_input_tokens", 0) or 0)
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached
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
        event_type = _get(chunk, "type")
        # message_start carries the input tokens and the model on a nested message object.
        message = _get(chunk, "message")
        if message is not None:
            model = _get(message, "model")
            if isinstance(model, str) and model:
                state["model"] = model
            start_usage = _get(message, "usage")
            if start_usage is not None:
                state["input_tokens"] = int(_get(start_usage, "input_tokens", 0) or 0)
                cached = _get(start_usage, "cache_read_input_tokens")
                if cached is not None:
                    state["cached_input_tokens"] = int(cached or 0)
                # A final message (used to seed usage when the caller never iterated events)
                # carries the full output count on the same usage object.
                message_output = _get(start_usage, "output_tokens")
                if message_output is not None:
                    state["output_tokens"] = int(message_output or 0)
        # message_delta carries the running output token count.
        delta_usage = _get(chunk, "usage")
        if delta_usage is not None and event_type != "message_start":
            output = _get(delta_usage, "output_tokens")
            if output is not None:
                state["output_tokens"] = int(output or 0)
            model = _get(chunk, "model")
            if isinstance(model, str) and model:
                state.setdefault("model", model)

    def finalize(self, state: dict) -> tuple[str | None, Usage | None]:
        if "input_tokens" not in state and "output_tokens" not in state:
            return state.get("model"), None
        usage = Usage(
            input_tokens=int(state.get("input_tokens", 0)),
            output_tokens=int(state.get("output_tokens", 0)),
            cached_input_tokens=int(state.get("cached_input_tokens", 0)),
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
