# SPDX-License-Identifier: Apache-2.0
"""Auto-instrumentation — provider call wrappers that emit conformant spans.

Implements CTO-48 (OpenAI first).

The design is pluggable: a :class:`ProviderInstrumentor` knows how to (a) extract token usage from a
provider response and (b) name the model/system. Everything else — building the conformant span via
:mod:`tally.schema`, pricing via :mod:`tally.pricing`, and the never-crash boundary — is shared, so
adding a new provider is just another extractor (no core change).

Instrumentation must NEVER swallow the provider's own exceptions (the customer needs their real
API errors). Only *our* span-building runs inside the safety boundary.
"""

from tally.instrumentation.base import (
    ProviderInstrumentor,
    Usage,
    build_span,
    wrap_create,
)
from tally.instrumentation.openai import OpenAIInstrumentor, instrument_openai_create

__all__ = [
    "ProviderInstrumentor",
    "Usage",
    "build_span",
    "wrap_create",
    "OpenAIInstrumentor",
    "instrument_openai_create",
]
