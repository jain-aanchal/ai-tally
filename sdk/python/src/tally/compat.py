# SPDX-License-Identifier: Apache-2.0
"""Provider compatibility matrix — generated from registered instrumentors.

Implements CTO-44. The matrix is built from instrumentor capabilities, not hand-maintained prose,
so it can't drift from what the code actually supports. Renders to a dict (for an API/page) and to
markdown (for docs).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from tally.instrumentation.openai import OpenAIInstrumentor


class Support(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    PLANNED = "planned"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What an instrumentor can capture for a provider."""

    provider: str
    token_usage: Support = Support.NONE
    cost: Support = Support.NONE
    streaming: Support = Support.NONE
    prompt_caching: Support = Support.NONE
    tool_calls: Support = Support.NONE
    models: tuple[str, ...] = ()

    @property
    def overall(self) -> Support:
        vals = [self.token_usage, self.cost, self.streaming, self.prompt_caching, self.tool_calls]
        if all(v is Support.FULL for v in vals):
            return Support.FULL
        if any(v in (Support.FULL, Support.PARTIAL) for v in vals):
            return Support.PARTIAL
        return Support.PLANNED


# Registry: capabilities declared against the *instrumentor's* provider name, so adding a provider
# instrumentor and its capabilities here keeps the matrix generated, not prose.
_REGISTRY: dict[str, Capabilities] = {}


def register(caps: Capabilities) -> None:
    _REGISTRY[caps.provider] = caps


def registered() -> dict[str, Capabilities]:
    return dict(_REGISTRY)


def render_matrix() -> list[dict[str, object]]:
    """Matrix as a list of row dicts (stable order by provider)."""
    return [
        {**asdict(caps), "overall": caps.overall.value}
        for _, caps in sorted(_REGISTRY.items())
    ]


def render_markdown() -> str:
    cols = [
        "provider", "overall", "token_usage", "cost", "streaming", "prompt_caching", "tool_calls"
    ]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for caps in sorted(_REGISTRY.values(), key=lambda c: c.provider):
        row = {
            "provider": caps.provider,
            "overall": caps.overall.value,
            "token_usage": caps.token_usage.value,
            "cost": caps.cost.value,
            "streaming": caps.streaming.value,
            "prompt_caching": caps.prompt_caching.value,
            "tool_calls": caps.tool_calls.value,
        }
        lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    return "\n".join(lines)


# --- built-in registrations (derived from shipped instrumentors) ---------------------------------

register(
    Capabilities(
        provider=OpenAIInstrumentor().system,  # "openai" — sourced from the instrumentor
        token_usage=Support.FULL,
        cost=Support.FULL,
        streaming=Support.FULL,
        prompt_caching=Support.FULL,
        tool_calls=Support.PARTIAL,  # accounted in usage; per-tool span attribution is a follow-up
        models=("gpt-5", "gpt-5-mini"),
    )
)
# Anthropic / Vertex instrumentors are follow-ups — declared planned so the matrix is honest.
register(Capabilities(provider="anthropic"))
register(Capabilities(provider="vertex"))
