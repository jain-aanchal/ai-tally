# SPDX-License-Identifier: Apache-2.0
"""Shared instrumentation machinery: provider interface, span builder, call wrapper."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import date
from typing import Protocol

from tally.context import current_context
from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd
from tally.safety import SelfObservability, safe_block
from tally.schema import SpanFields, build_span_attributes


class ProviderInstrumentor(Protocol):
    """Per-provider knowledge. Pure functions over a provider response object."""

    system: str

    def request_model(self, args: tuple, kwargs: dict) -> str | None: ...
    def response_model(self, response: object) -> str | None: ...
    def extract_usage(self, response: object) -> Usage: ...


def build_span(
    instrumentor: ProviderInstrumentor,
    *,
    args: tuple,
    kwargs: dict,
    response: object,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    at: date | None = None,
) -> dict[str, object]:
    """Build a conformant span attribute dict from a provider response.

    Pulls feature_tag/session from the active trace context, computes cost from the catalog (if
    given), and returns attributes guaranteed to pass ``validate_span_attributes``.
    """
    usage = instrumentor.extract_usage(response)
    req_model = instrumentor.request_model(args, kwargs)
    resp_model = instrumentor.response_model(response) or req_model
    ctx = current_context()

    cost_micro: int | None = None
    catalog_version: str | None = None
    if catalog is not None and resp_model:
        cost_micro, version = compute_cost_micro_usd(
            catalog, instrumentor.system, resp_model, usage, at=at, tenant_id=tenant_id
        )
        catalog_version = version or None

    fields = SpanFields(
        system=instrumentor.system,
        request_model=req_model,
        response_model=resp_model,
        operation="chat",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens or None,
        cost_estimated_micro_usd=cost_micro,
        price_catalog_version=catalog_version,
        feature_tag=ctx.feature_tag,
        session_id=ctx.session_id,
    )
    return build_span_attributes(fields)


def wrap_create(
    create_fn: Callable[..., object],
    instrumentor: ProviderInstrumentor,
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
) -> Callable[..., object]:
    """Wrap a provider ``create``-style callable so each successful call emits a span.

    The provider call itself is NOT guarded — its exceptions propagate to the caller unchanged.
    Only span building + ``on_span`` run inside the safety boundary, so instrumentation can never
    break the customer's call.
    """
    observ = obs or SelfObservability()

    @functools.wraps(create_fn)
    def wrapper(*args, **kwargs):
        response = create_fn(*args, **kwargs)  # provider errors propagate — by design
        with safe_block(observ, where=f"instrument.{instrumentor.system}"):
            attrs = build_span(
                instrumentor,
                args=args,
                kwargs=kwargs,
                response=response,
                catalog=catalog,
                tenant_id=tenant_id,
            )
            on_span(attrs)
        return response

    return wrapper
