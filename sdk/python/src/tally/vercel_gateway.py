# SPDX-License-Identifier: Apache-2.0
"""Vercel AI Gateway instrumentation (CTO-161).

An app can route its model calls through the **Vercel AI Gateway**, which proxies OpenAI /
Anthropic / Google / Bedrock / ... behind a single OpenAI-compatible endpoint. If ai-tally
recorded such a call naively it would either lose the spend behind the gateway hop or mislabel
it generically ``"vercel"`` — neither of which prices correctly against the catalog.

This module maps the **gateway response metadata** back onto the TRUE upstream provider + model,
so a call proxied through the gateway records a span whose ``gen_ai.system`` /
``gen_ai.request.model`` resolve to (e.g.) ``openai`` / ``gpt-4o-mini``. The gateway does no
special-casing downstream: :func:`tally.enrichment.enrich_cost` prices the span straight from the
catalog exactly as it does for a direct SDK call (same as the CTO-149 / CTO-157 provider paths).

Resolution (see :func:`resolve_upstream`):

* **provider** — from an explicit provider slug in the metadata, else the prefix of the
  namespaced model id (Vercel model ids are ``"<creator>/<model>"``, e.g. ``"openai/gpt-4o-mini"``).
  Creator slugs are normalised to the catalog's provider keys via :data:`_PROVIDER_ALIASES`.
* **model** — the bare model id (the part after ``"<creator>/"``), or an explicit model field.
* **usage** — the gateway's own token counts are PREFERRED (OpenAI-compatible
  ``prompt_tokens`` / ``completion_tokens`` / ``prompt_tokens_details.cached_tokens`` and the
  AI-SDK ``inputTokens`` / ``outputTokens`` / ``cachedInputTokens`` shapes are both accepted). If
  the gateway omits usage, an optional caller-supplied ``fallback_usage`` (token-counted at the
  call site — never a message body) is used instead.

**Unknown-safe:** if the upstream provider/model can't be resolved from the metadata, the call is
attributed to :data:`UNKNOWN` (``"unknown"``). The catalog has no ``unknown`` price, so
``enrich_cost`` reports ``catalog_miss`` and the cost lands null — the dashboard renders ``—``.
We NEVER guess a provider or fabricate a model id.

Counts only, never bodies: this helper reads token counts and model ids from response metadata; it
never touches prompt/completion text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tally.pricing import Usage

#: Attribution used when the upstream provider/model can't be resolved. The catalog prices nothing
#: under this key, so cost enriches to null (dashboard ``—``) rather than a fabricated number.
UNKNOWN = "unknown"

# Vercel AI Gateway creator slug -> ai-tally catalog provider key. The gateway namespaces model ids
# by the model's *creator* (e.g. "openai/gpt-4o-mini"); the catalog keys spend by provider. For the
# vendor-direct creators the slug already matches the catalog key. The Amazon/Vertex aliases map the
# managed-runtime creators onto the provider dimension the catalog prices them under.
_PROVIDER_ALIASES: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "google-vertex": "google",
    "vertex": "google",
    "amazon": "bedrock",
    "aws": "bedrock",
    "bedrock": "bedrock",
}


@dataclass(frozen=True, slots=True)
class UpstreamAttribution:
    """Resolved true-upstream attribution for a gateway-proxied call.

    ``resolved`` is False when the provider/model couldn't be determined from the metadata; in that
    case ``provider`` is :data:`UNKNOWN` and callers get a null cost rather than a guessed one.
    """

    provider: str
    model: str
    usage: Usage
    resolved: bool


def _as_mapping(v: object) -> Mapping[str, Any] | None:
    return v if isinstance(v, Mapping) else None


def _first_str(md: Mapping[str, Any], *keys: str) -> str | None:
    """First non-empty string value among ``keys`` (top level only)."""
    for k in keys:
        v = md.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _int_or_none(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _gateway_meta(md: Mapping[str, Any]) -> Mapping[str, Any]:
    """The nested ``providerMetadata.gateway`` block, if present, else an empty mapping.

    The AI SDK surfaces gateway routing/usage info under ``providerMetadata.gateway`` (snake_case
    ``provider_metadata`` also accepted). Falls back to any top-level ``gateway`` block.
    """
    pm = _as_mapping(md.get("providerMetadata")) or _as_mapping(md.get("provider_metadata"))
    if pm is not None:
        gw = _as_mapping(pm.get("gateway"))
        if gw is not None:
            return gw
    return _as_mapping(md.get("gateway")) or {}


def _resolve_provider_model(md: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(provider, model)`` resolved from the metadata; either may be None if absent.

    Precedence for the model id: explicit ``model`` / ``modelId`` at the top level, else the
    gateway block's ``model`` / ``modelId``. A namespaced ``"<creator>/<model>"`` id splits into a
    creator slug (mapped to a catalog provider) and the bare model. An explicit provider slug
    (top-level or gateway ``provider``) overrides the creator prefix.
    """
    gw = _gateway_meta(md)
    model_id = _first_str(md, "model", "modelId") or _first_str(gw, "model", "modelId")
    explicit_provider = _first_str(md, "provider") or _first_str(gw, "provider")

    creator: str | None = None
    model: str | None = None
    if model_id is not None:
        if "/" in model_id:
            creator, _, model = model_id.partition("/")
            creator = creator or None
            model = model or None
        else:
            model = model_id

    slug = explicit_provider or creator
    provider = _PROVIDER_ALIASES.get(slug.lower()) if slug is not None else None
    # An unknown slug is still a real provider signal we shouldn't discard silently — but we won't
    # invent a catalog key for it. Pass it through verbatim; the catalog decides if it prices.
    if provider is None and slug is not None:
        provider = slug.lower()
    return provider, model


def _parse_usage(md: Mapping[str, Any]) -> Usage | None:
    """Extract the gateway's own usage numbers, or None if it reported none.

    Accepts both the OpenAI-compatible shape (``prompt_tokens`` / ``completion_tokens`` /
    ``prompt_tokens_details.cached_tokens``) and the AI-SDK shape (``inputTokens`` /
    ``outputTokens`` / ``cachedInputTokens``). Usage may sit at the top level or inside a nested
    ``usage`` block (also checked under the gateway block).
    """
    gw = _gateway_meta(md)
    for source in (
        md,
        _as_mapping(md.get("usage")) or {},
        gw,
        _as_mapping(gw.get("usage")) or {},
    ):
        inp = _int_or_none(source.get("prompt_tokens"))
        if inp is None:
            inp = _int_or_none(source.get("inputTokens"))
        out = _int_or_none(source.get("completion_tokens"))
        if out is None:
            out = _int_or_none(source.get("outputTokens"))
        if inp is None and out is None:
            continue
        cached = _int_or_none(source.get("cachedInputTokens"))
        if cached is None:
            details = _as_mapping(source.get("prompt_tokens_details")) or {}
            cached = _int_or_none(details.get("cached_tokens"))
        return Usage(
            input_tokens=inp or 0,
            output_tokens=out or 0,
            cached_input_tokens=cached or 0,
        )
    return None


def resolve_upstream(
    metadata: Mapping[str, Any],
    *,
    fallback_usage: Usage | None = None,
) -> UpstreamAttribution:
    """Map Vercel AI Gateway response metadata onto true-upstream ``(provider, model, usage)``.

    Prefers the gateway's own usage numbers; if it reports none, uses ``fallback_usage`` (which the
    caller may have token-counted locally — never a message body), else zero usage. When the
    provider or model can't be resolved, attributes to :data:`UNKNOWN` with ``resolved=False`` so
    the cost enriches to null rather than a guess.
    """
    provider, model = _resolve_provider_model(metadata)
    usage = _parse_usage(metadata) or fallback_usage or Usage(0, 0, 0)

    if not provider or not model:
        # Can't price what we can't name — attribute to unknown, never guess.
        return UpstreamAttribution(
            provider=provider or UNKNOWN,
            model=model or UNKNOWN,
            usage=usage,
            resolved=False,
        )
    return UpstreamAttribution(provider=provider, model=model, usage=usage, resolved=True)


def record_gateway_llm_call(
    client: Any,
    metadata: Mapping[str, Any],
    *,
    fallback_usage: Usage | None = None,
    signals: Any = None,
    at: Any = None,
) -> Any:
    """Resolve true-upstream attribution from ``metadata`` and record it via ``client``.

    Thin convenience over :func:`resolve_upstream` + ``TallyClient.record_llm_call`` so a
    gateway-proxied call is a one-liner. Returns the ``LlmCallResult``. Unknown upstreams flow
    through as ``provider="unknown"`` (cost null) — the call is still recorded, never dropped.
    """
    att = resolve_upstream(metadata, fallback_usage=fallback_usage)
    return client.record_llm_call(
        provider=att.provider,
        model=att.model,
        usage=att.usage,
        signals=signals,
        at=at,
    )
