# SPDX-License-Identifier: Apache-2.0
"""Auto-instrumentation — provider call wrappers that emit conformant spans.

Implements CTO-48 (OpenAI first) and CTO-260 (the one-line ``tally.init`` path: Anthropic, async,
streaming, and the ``patch_openai`` / ``patch_anthropic`` monkeypatchers).

The design is pluggable: a :class:`ProviderInstrumentor` knows how to (a) extract token usage from a
provider response and (b) name the model/system/operation. Everything else — building the conformant
span via :mod:`tally.schema`, pricing via :mod:`tally.pricing`, and the never-crash boundary — is
shared, so adding a new provider is just another extractor (no core change).

Instrumentation must NEVER swallow the provider's own exceptions (the customer needs their real
API errors). Only *our* span-building runs inside the safety boundary.
"""

from tally.instrumentation.anthropic import (
    AnthropicInstrumentor,
    instrument_anthropic_create,
)
from tally.instrumentation.base import (
    ProviderInstrumentor,
    StreamInstrumentor,
    Usage,
    build_span,
    wrap_create,
    wrap_create_async,
    wrap_stream,
    wrap_stream_async,
)
from tally.instrumentation.openai import (
    OpenAIEmbeddingsInstrumentor,
    OpenAIInstrumentor,
    OpenAIResponsesInstrumentor,
    instrument_openai_create,
)
from tally.instrumentation.patch import (
    patch_anthropic,
    patch_openai,
    unpatch_all,
)

__all__ = [
    "ProviderInstrumentor",
    "StreamInstrumentor",
    "Usage",
    "build_span",
    "wrap_create",
    "wrap_create_async",
    "wrap_stream",
    "wrap_stream_async",
    "OpenAIInstrumentor",
    "OpenAIResponsesInstrumentor",
    "OpenAIEmbeddingsInstrumentor",
    "instrument_openai_create",
    "AnthropicInstrumentor",
    "instrument_anthropic_create",
    "patch_openai",
    "patch_anthropic",
    "unpatch_all",
]
