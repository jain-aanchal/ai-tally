# SPDX-License-Identifier: Apache-2.0
"""OpenAI instrumentor — extracts usage from a Chat Completions response.

Works with both the SDK's object responses and plain dicts, so it can be tested with a fake client
and no network. Reads ``usage.prompt_tokens`` / ``usage.completion_tokens`` and
``usage.prompt_tokens_details.cached_tokens`` when present.
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


class OpenAIInstrumentor:
    system = "openai"

    def request_model(self, args: tuple, kwargs: dict) -> str | None:
        return kwargs.get("model")

    def response_model(self, response: object) -> str | None:
        model = _get(response, "model")
        return model if isinstance(model, str) else None

    def extract_usage(self, response: object) -> Usage:
        usage = _get(response, "usage")
        prompt = int(_get(usage, "prompt_tokens", 0) or 0)
        completion = int(_get(usage, "completion_tokens", 0) or 0)
        details = _get(usage, "prompt_tokens_details")
        cached = int(_get(details, "cached_tokens", 0) or 0)
        return Usage(
            input_tokens=prompt, output_tokens=completion, cached_input_tokens=cached
        )


# satisfy the Protocol at import time (structural; this is a no-op assertion for readers)
_INSTRUMENTOR: ProviderInstrumentor = OpenAIInstrumentor()


def instrument_openai_create(
    create_fn: Callable[..., object],
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
) -> Callable[..., object]:
    """Wrap ``client.chat.completions.create`` so each call emits a conformant span.

    Example::

        client.chat.completions.create = instrument_openai_create(
            client.chat.completions.create, on_span=tally_client.ingest_span, catalog=catalog
        )
    """
    return wrap_create(
        create_fn,
        OpenAIInstrumentor(),
        on_span=on_span,
        obs=obs,
        catalog=catalog,
        tenant_id=tenant_id,
    )
