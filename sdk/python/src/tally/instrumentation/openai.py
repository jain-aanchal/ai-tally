# SPDX-License-Identifier: Apache-2.0
"""OpenAI instrumentors — Chat Completions (CTO-48), Responses, and Embeddings (CTO-260 §4.1/§4.2).

Every instrumentor is a pure function over a provider response object and works with both the SDK's
object responses and plain dicts, so it can be tested with a fake client and no network. Nothing
here reads message content or secrets — only ``usage`` / ``model`` metadata.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from tally.instrumentation.base import ProviderInstrumentor, wrap_create
from tally.pricing import (
    PriceCatalog,
    Usage,
    compute_cost_micro_usd,
    compute_embedding_cost_micro_usd,
)
from tally.safety import SelfObservability


def _get(obj: object, key: str, default: object = None) -> object:
    """Attribute-or-key accessor (supports SDK objects and dicts)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class OpenAIInstrumentor:
    """Chat Completions. Reads ``usage.prompt_tokens`` / ``usage.completion_tokens`` and
    ``usage.prompt_tokens_details.cached_tokens`` when present.

    Also serves streamed chat (``stream=True``) via :meth:`accumulate` / :meth:`finalize`:
    OpenAI reports usage only in the terminal chunk (when ``stream_options.include_usage`` is set),
    so usage stays ``None`` until that chunk arrives (CTO-260 §4.3).
    """

    system = "openai"
    operation = "chat"

    def request_model(self, args: tuple, kwargs: dict) -> str | None:
        return kwargs.get("model")

    def response_model(self, response: object) -> str | None:
        model = _get(response, "model")
        return model if isinstance(model, str) else None

    def extract_usage(self, response: object) -> Usage | None:
        usage = _get(response, "usage")
        if usage is None:
            return None
        prompt = int(_get(usage, "prompt_tokens", 0) or 0)
        completion = int(_get(usage, "completion_tokens", 0) or 0)
        details = _get(usage, "prompt_tokens_details")
        cached = int(_get(details, "cached_tokens", 0) or 0)
        return Usage(
            input_tokens=prompt, output_tokens=completion, cached_input_tokens=cached
        )

    # --- streaming (CTO-260 §4.3) ---
    def accumulate(self, state: dict, chunk: object) -> None:
        model = _get(chunk, "model")
        if isinstance(model, str) and model:
            state["model"] = model
        usage = _get(chunk, "usage")
        if usage is not None:
            state["usage"] = usage

    def finalize(self, state: dict) -> tuple[str | None, Usage | None]:
        usage_obj = state.get("usage")
        usage = self.extract_usage({"usage": usage_obj}) if usage_obj is not None else None
        return state.get("model"), usage


class OpenAIResponsesInstrumentor:
    """OpenAI Responses API (``client.responses.create``). Usage under
    ``response.usage.input_tokens`` / ``output_tokens`` (CTO-260 §4.2)."""

    system = "openai"
    operation = "chat"

    def request_model(self, args: tuple, kwargs: dict) -> str | None:
        return kwargs.get("model")

    def response_model(self, response: object) -> str | None:
        model = _get(response, "model")
        return model if isinstance(model, str) else None

    def extract_usage(self, response: object) -> Usage | None:
        usage = _get(response, "usage")
        if usage is None:
            return None
        # The Responses API names these input_tokens / output_tokens (unlike Chat Completions).
        input_tokens = int(_get(usage, "input_tokens", 0) or 0)
        output_tokens = int(_get(usage, "output_tokens", 0) or 0)
        details = _get(usage, "input_tokens_details")
        cached = int(_get(details, "cached_tokens", 0) or 0)
        return Usage(
            input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached
        )


class OpenAIEmbeddingsInstrumentor:
    """OpenAI Embeddings (``client.embeddings.create``). ``usage.prompt_tokens`` is the input;
    there are no output tokens, and cost prices under ``PriceType.EMBEDDING`` (CTO-260 §4.2)."""

    system = "openai"
    operation = "embeddings"

    def request_model(self, args: tuple, kwargs: dict) -> str | None:
        return kwargs.get("model")

    def response_model(self, response: object) -> str | None:
        model = _get(response, "model")
        return model if isinstance(model, str) else None

    def extract_usage(self, response: object) -> Usage | None:
        usage = _get(response, "usage")
        if usage is None:
            return None
        prompt = int(_get(usage, "prompt_tokens", 0) or 0)
        return Usage(input_tokens=prompt, output_tokens=0)

    def compute_cost(
        self,
        catalog: PriceCatalog,
        model: str,
        usage: Usage,
        *,
        at: date | None = None,
        tenant_id: str | None = None,
    ) -> tuple[int, str]:
        return compute_embedding_cost_micro_usd(
            catalog, self.system, model, usage.input_tokens, at=at, tenant_id=tenant_id
        )


# satisfy the Protocol at import time (structural; this is a no-op assertion for readers)
_INSTRUMENTOR: ProviderInstrumentor = OpenAIInstrumentor()

# Compat: some call sites reference the chat cost path directly.
_ = compute_cost_micro_usd


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
