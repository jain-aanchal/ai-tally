# SPDX-License-Identifier: Apache-2.0
"""TallyClient — the SDK entrypoint.

Ties the spine together: schema (CTO-47) + safety (CTO-45) + context (CTO-46) + sampling (CTO-50)
+ pricing (CTO-52) + egress (CTO-49), with a cohesive high-level ``record_llm_call()`` API.

Every public method runs inside the safety boundary so a bug in the SDK — or a pluggable
exporter/transport — never escapes into the customer's code path. Guardrail *enforcement* is the
one intentional exception and lives behind :meth:`guard` (it may raise, by design, for the agent
framework to catch); ``record_llm_call`` itself never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from tally.context import current_context, note_context_drop
from tally.egress import BatchProcessor
from tally.guardrails import GuardrailConfig, GuardrailEngine, GuardrailState, Verdict
from tally.pricing import (
    PriceCatalog,
    Usage,
    compute_cost_micro_usd,
    compute_embedding_cost_micro_usd,
)
from tally.safety import SelfObservability, safe
from tally.sampling import BillingMeter, Sampler, TraceSignals
from tally.schema import SpanFields, build_span_attributes

_log = logging.getLogger("tally")

# Default per-(provider, tool) tool-call prices in micro-USD (CTO-135). Inline and hand-maintained
# until a real tool-pricing catalog lands (tracked by CTO-141). When a caller omits an explicit
# ``cost_micro_usd`` we look the pair up here; an unknown pair defaults to 0 with a one-time WARN.
_TOOL_PRICING: dict[tuple[str, str], int] = {
    ("tavily", "search"): 10_000,
    ("serpapi", "search"): 15_000,
    ("brave", "search"): 5_000,
    ("firecrawl", "scrape"): 20_000,
}

# Pairs we've already warned about, so the missing-price WARN fires once per (provider, tool).
_warned_tool_pairs: set[tuple[str, str]] = set()
# Embedding (provider, model) pairs we've already warned about (CTO-136).
_warned_embedding_pairs: set[tuple[str, str]] = set()


class Exporter(Protocol):
    def export(self, attributes: dict[str, object]) -> None: ...


class MemoryExporter:
    """Default no-network exporter: keeps spans in a list. Useful for tests and local dev."""

    def __init__(self) -> None:
        self.spans: list[dict[str, object]] = []

    def export(self, attributes: dict[str, object]) -> None:
        self.spans.append(attributes)


@dataclass(frozen=True, slots=True)
class LlmCallResult:
    trace_id: str | None
    cost_micro_usd: int | None
    kept: bool
    sample_rate: float
    attributes: dict[str, object]


@dataclass(frozen=True, slots=True)
class EmbeddingCallResult:
    """Result of :meth:`TallyClient.record_embedding_call` (CTO-136).

    Mirrors :class:`LlmCallResult` but without sampling fields — embeddings always emit.
    """

    trace_id: str | None
    cost_micro_usd: int | None
    attributes: dict[str, object]


class TallyClient:
    """Customer-facing entrypoint.

    Args:
        api_key / endpoint: stored for egress wiring.
        exporter: simple span sink (used when no ``processor`` is given).
        processor: :class:`BatchProcessor` for real egress (buffer/batch/backoff). Takes
            precedence over ``exporter`` when both are set.
        catalog: price catalog for server-agnostic cost estimation.
        sampler / billing_meter / guardrails: spine components (sensible defaults).
        tenant_id: for per-tenant price overrides.
    """

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        *,
        exporter: Exporter | None = None,
        processor: BatchProcessor | None = None,
        catalog: PriceCatalog | None = None,
        sampler: Sampler | None = None,
        billing_meter: BillingMeter | None = None,
        guardrails: GuardrailEngine | None = None,
        observability: SelfObservability | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.obs = observability or SelfObservability()
        self._api_key = api_key
        self._endpoint = endpoint
        self._processor = processor
        self._exporter: Exporter = exporter or MemoryExporter()
        self.catalog = catalog
        self.sampler = sampler or Sampler()
        self.billing = billing_meter or BillingMeter()
        self.guardrails = guardrails or GuardrailEngine()
        self.tenant_id = tenant_id

    @property
    def observability(self) -> SelfObservability:
        return self.obs

    # --- low-level: record a pre-built span ---
    def record_span(self, fields: SpanFields) -> None:
        """Record one span from explicit fields. Never raises."""

        @safe(self.obs, where="TallyClient.record_span")
        def _do() -> None:
            self._emit(build_span_attributes(fields))

        _do()

    def ingest_span(self, attributes: dict[str, object]) -> None:
        """Sink for instrumentation (CTO-48 ``on_span``). Never raises."""

        @safe(self.obs, where="TallyClient.ingest_span")
        def _do() -> None:
            self._emit(attributes)

        _do()

    # --- high-level: record an LLM call (cost + sampling + billing + egress) ---
    def record_llm_call(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage,
        signals: TraceSignals | None = None,
        at: date | None = None,
    ) -> LlmCallResult:
        """Record an LLM call end-to-end. Never raises.

        Steps (all inside the safety boundary):
          1. read trace context (note a drop if no active trace),
          2. count the trace for billing at HEAD (before sampling),
          3. estimate cost from the catalog,
          4. build a conformant span,
          5. make the sampling decision; emit the span only if kept,
          6. return a :class:`LlmCallResult` for the caller.
        """

        @safe(self.obs, where="TallyClient.record_llm_call", fallback=None)
        def _do() -> LlmCallResult:
            ctx = current_context()
            trace_id = ctx.trace_id
            if trace_id is None:
                note_context_drop(self.obs, where="record_llm_call")

            # Billing counts at HEAD, before sampling (CTO-50/CTO-84).
            if trace_id is not None:
                self.billing.count_trace(trace_id)

            cost_micro: int | None = None
            catalog_version: str | None = None
            if self.catalog is not None:
                cost_micro, version = compute_cost_micro_usd(
                    self.catalog, provider, model, usage, at=at, tenant_id=self.tenant_id
                )
                catalog_version = version or None

            decision = self.sampler.decide(
                trace_id or "no-trace", signals, feature_tag=ctx.feature_tag
            )

            fields = SpanFields(
                system=provider,
                request_model=model,
                response_model=model,
                operation="chat",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens or None,
                cost_estimated_micro_usd=cost_micro,
                price_catalog_version=catalog_version,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
                # CTO-119: stratum + configured keep-rate ride on the kept span so the DQ surface
                # can compute per-stratum CIs without re-classifying after the fact.
                sampling_stratum=decision.stratum.value,
                sampling_rate=decision.sample_rate,
            )
            attrs = build_span_attributes(fields)
            # NB: sample_rate travels at the batch level (wire Sampling, §12.2), not as a span
            # attribute — so the span stays schema-conformant. It's returned in the result.
            if decision.keep:
                self._emit(attrs)

            return LlmCallResult(
                trace_id=trace_id,
                cost_micro_usd=cost_micro,
                kept=decision.keep,
                sample_rate=decision.sample_rate,
                attributes=attrs,
            )

        result = _do()
        if result is None:  # boundary swallowed an error; return a benign result
            return LlmCallResult(None, None, False, 1.0, {})
        return result

    # --- high-level: record a tool call (Tools cost layer, CTO-135) ---
    def record_tool_call(
        self,
        *,
        provider: str,
        tool: str,
        cost_micro_usd: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        call_id: str | None = None,
    ) -> None:
        """Record a tool call so the span lands in the gateway's ``tools`` cost-layer bucket.

        Bucketing is keyed off ``gen_ai.operation.name == 'tool'``. The tool's cost rides on
        ``gen_ai.tool.cost_micro_usd``. When ``cost_micro_usd`` is omitted we resolve a default
        from the inline :data:`_TOOL_PRICING` table (CTO-141); an unknown ``(provider, tool)``
        pair defaults to 0 with a one-time WARN. Never raises.

        ``latency_ms`` is accepted for API symmetry with future span timing but is not yet emitted
        as a schema attribute (no latency key exists in the conformant set).
        """

        @safe(self.obs, where="TallyClient.record_tool_call")
        def _do() -> None:
            ctx = current_context()
            if ctx.trace_id is None:
                note_context_drop(self.obs, where="record_tool_call")

            resolved_cost = cost_micro_usd
            if resolved_cost is None:
                key = (provider, tool)
                resolved_cost = _TOOL_PRICING.get(key)
                if resolved_cost is None:
                    if key not in _warned_tool_pairs:
                        _warned_tool_pairs.add(key)
                        _log.warning(
                            "no default tool price for (%s, %s); defaulting cost to 0",
                            provider,
                            tool,
                        )
                    resolved_cost = 0

            fields = SpanFields(
                system=provider,
                operation="tool",
                tool_name=tool,
                tool_call_id=call_id,
                tool_cost_micro_usd=resolved_cost,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
            )
            self._emit(build_span_attributes(fields))

        _do()

    # --- high-level: record an embedding call (Embeddings cost layer, CTO-136) ---
    def record_embedding_call(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        at: date | None = None,
    ) -> EmbeddingCallResult:
        """Record an embedding call so the span lands in the gateway's ``embeddings`` bucket.

        Bucketing is keyed off ``gen_ai.operation.name == 'embeddings'``. Cost is estimated from
        the catalog (input-side only). Unknown provider/model → cost stays None/0 with a one-time
        WARN. Never raises.
        """

        @safe(self.obs, where="TallyClient.record_embedding_call", fallback=None)
        def _do() -> EmbeddingCallResult:
            ctx = current_context()
            trace_id = ctx.trace_id
            if trace_id is None:
                note_context_drop(self.obs, where="record_embedding_call")

            cost_micro: int | None = None
            catalog_version: str | None = None
            if self.catalog is not None:
                # Embeddings are priced under PriceType.EMBEDDING, not INPUT — use the
                # embedding-specific resolver so seeded embedding rates actually apply.
                cost_micro, version = compute_embedding_cost_micro_usd(
                    self.catalog,
                    provider,
                    model,
                    input_tokens,
                    at=at,
                    tenant_id=self.tenant_id,
                )
                catalog_version = version or None
                if not catalog_version:
                    # No applicable rate → partial/zero price. Warn once per (provider, model).
                    key = (provider, model)
                    if key not in _warned_embedding_pairs:
                        _warned_embedding_pairs.add(key)
                        _log.warning(
                            "no embedding price for (%s, %s); cost estimated as 0",
                            provider,
                            model,
                        )

            fields = SpanFields(
                system=provider,
                request_model=model,
                operation="embeddings",
                input_tokens=input_tokens,
                cost_estimated_micro_usd=cost_micro,
                price_catalog_version=catalog_version,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
            )
            attrs = build_span_attributes(fields)
            self._emit(attrs)

            return EmbeddingCallResult(
                trace_id=trace_id,
                cost_micro_usd=cost_micro,
                attributes=attrs,
            )

        result = _do()
        if result is None:  # boundary swallowed an error; return a benign result
            return EmbeddingCallResult(None, None, {})
        return result

    # --- guardrails (may raise, by design — pre-call check) ---
    def guard(self, state: GuardrailState, config: GuardrailConfig) -> Verdict:
        """Consult guardrails before the next call. May raise CostLimitExceededException in
        GRACEFUL/HARD_STOP modes — that propagation is intentional (the agent framework catches it
        and degrades). Not wrapped in the safety boundary."""
        return self.guardrails.evaluate(state, config)

    def _emit(self, attributes: dict[str, object]) -> None:
        if self._processor is not None:
            self._processor.enqueue(attributes)
        else:
            self._exporter.export(attributes)
