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
from tally.hmac_keys import HmacKeyRegistry
from tally.pricing import (
    PriceCatalog,
    PriceType,
    Usage,
    compute_call_cost_micro_usd,
    compute_cost_micro_usd,
    compute_embedding_cost_micro_usd,
)
from tally.safety import SelfObservability, safe
from tally.sampling import BillingMeter, Sampler, TraceSignals
from tally.schema import SpanFields, build_span_attributes

_log = logging.getLogger("tally")

# Tool + vector per-call prices now live in the versioned price catalog (CTO-141) under
# PriceType.TOOL_CALL / PriceType.VECTOR_CALL — the inline ``_TOOL_PRICING`` / ``_VECTOR_PRICING``
# stopgap dicts (PR #111 / #116) were removed. ``record_tool_call`` / ``record_vector_call`` resolve
# the rate via ``compute_call_cost_micro_usd`` when the caller omits ``cost_micro_usd``.

# Pairs we've already warned about, so the missing-price WARN fires once per (provider, tool).
_warned_tool_pairs: set[tuple[str, str]] = set()
# Embedding (provider, model) pairs we've already warned about (CTO-136).
_warned_embedding_pairs: set[tuple[str, str]] = set()
# Vector (provider, operation) pairs we've already warned about (CTO-142).
_warned_vector_pairs: set[tuple[str, str]] = set()
# Tenants we've already warned about for an unhashable account id (CTO-181). ``None`` covers the
# no-tenant case. One WARN per tenant, not one per span.
_warned_account_tenants: set[str | None] = set()


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
        tenant_id: for per-tenant price overrides and for the per-tenant HMAC key.
        hmac_registry: key registry used to hash account ids (CTO-181). Required together with
            ``tenant_id`` for the account dimension to be emitted; without both, account tagging
            degrades to a one-time WARN and the span is emitted unattributed rather than dropped.
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
        hmac_registry: HmacKeyRegistry | None = None,
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
        self.hmac_registry = hmac_registry

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
        account_id: str | None = None,
        account_label: str | None = None,
    ) -> LlmCallResult:
        """Record an LLM call end-to-end. Never raises.

        ``provider`` is a free-form string used verbatim as ``gen_ai.system`` and as the
        catalog lookup key — there is no provider allowlist, so any provider the catalog
        prices (``"openai"``, ``"anthropic"``, ``"google"``, ...) works. Gemini / Vertex AI
        callers pass ``provider="google"`` and map the Google usage fields onto ``Usage``:
        ``promptTokenCount`` -> ``input_tokens``, ``candidatesTokenCount`` -> ``output_tokens``,
        ``cachedContentTokenCount`` -> ``cached_input_tokens`` (CTO-149).

        Steps (all inside the safety boundary):
          1. read trace context (note a drop if no active trace),
          2. count the trace for billing at HEAD (before sampling),
          3. estimate cost from the catalog,
          4. build a conformant span,
          5. make the sampling decision; emit the span only if kept,
          6. return a :class:`LlmCallResult` for the caller.

        ``account_id`` tags the span with the tenant's own customer (CTO-181). Omit it and the
        account set by :func:`~tally.context.with_account` for the surrounding scope applies;
        pass it to override that for this one call. It is hashed with the tenant's HMAC key and
        never travels raw. ``account_label`` is optional, wire-only, and only carried when a
        hash was produced.
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

            acct_hash, acct_version, acct_label = self._resolve_account(account_id, account_label)

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
                account_id_hash=acct_hash,
                account_id_hash_key_version=acct_version,
                account_label=acct_label,
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
        account_id: str | None = None,
        account_label: str | None = None,
    ) -> None:
        """Record a tool call so the span lands in the gateway's ``tools`` cost-layer bucket.

        Bucketing is keyed off ``gen_ai.operation.name == 'tool'``. The tool's cost rides on
        ``gen_ai.tool.cost_micro_usd``. When ``cost_micro_usd`` is omitted we resolve the rate from
        the versioned price catalog (CTO-141) under ``PriceType.TOOL_CALL`` and stamp
        ``price_catalog_version`` on the span; an unknown ``(provider, tool)`` pair (or no catalog)
        defaults to 0 with a one-time WARN. A caller-supplied ``cost_micro_usd`` always overrides.
        Never raises.

        ``latency_ms`` is accepted for API symmetry with future span timing but is not yet emitted
        as a schema attribute (no latency key exists in the conformant set).

        ``account_id`` tags the span with the tenant's own customer (CTO-181). Omit it and the
        account set by :func:`~tally.context.with_account` for the surrounding scope applies;
        pass it to override that for this one call. It is hashed with the tenant's HMAC key and
        never travels raw. ``account_label`` is optional, wire-only, and only carried when a
        hash was produced.
        """

        @safe(self.obs, where="TallyClient.record_tool_call")
        def _do() -> None:
            ctx = current_context()
            if ctx.trace_id is None:
                note_context_drop(self.obs, where="record_tool_call")

            resolved_cost = cost_micro_usd
            catalog_version: str | None = None
            if resolved_cost is None:
                key = (provider, tool)
                if self.catalog is not None:
                    resolved_cost, version = compute_call_cost_micro_usd(
                        self.catalog,
                        provider,
                        tool,
                        PriceType.TOOL_CALL,
                        tenant_id=self.tenant_id,
                    )
                    catalog_version = version or None
                else:
                    resolved_cost = 0
                if not catalog_version:
                    resolved_cost = 0
                    if key not in _warned_tool_pairs:
                        _warned_tool_pairs.add(key)
                        _log.warning(
                            "no catalog tool price for (%s, %s); defaulting cost to 0",
                            provider,
                            tool,
                        )

            acct_hash, acct_version, acct_label = self._resolve_account(account_id, account_label)
            fields = SpanFields(
                system=provider,
                operation="tool",
                tool_name=tool,
                tool_call_id=call_id,
                tool_cost_micro_usd=resolved_cost,
                price_catalog_version=catalog_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
                account_id_hash=acct_hash,
                account_id_hash_key_version=acct_version,
                account_label=acct_label,
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
        account_id: str | None = None,
        account_label: str | None = None,
    ) -> EmbeddingCallResult:
        """Record an embedding call so the span lands in the gateway's ``embeddings`` bucket.

        Bucketing is keyed off ``gen_ai.operation.name == 'embeddings'``. Cost is estimated from
        the catalog (input-side only). Unknown provider/model → cost stays None/0 with a one-time
        WARN. Never raises.

        ``account_id`` tags the span with the tenant's own customer (CTO-181). Omit it and the
        account set by :func:`~tally.context.with_account` for the surrounding scope applies;
        pass it to override that for this one call. It is hashed with the tenant's HMAC key and
        never travels raw. ``account_label`` is optional, wire-only, and only carried when a
        hash was produced.
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

            acct_hash, acct_version, acct_label = self._resolve_account(account_id, account_label)
            fields = SpanFields(
                system=provider,
                request_model=model,
                operation="embeddings",
                input_tokens=input_tokens,
                cost_estimated_micro_usd=cost_micro,
                price_catalog_version=catalog_version,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
                account_id_hash=acct_hash,
                account_id_hash_key_version=acct_version,
                account_label=acct_label,
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

    # --- high-level: record a vector-DB call (Vector cost layer, CTO-142) ---
    def record_vector_call(
        self,
        *,
        provider: str,
        index: str,
        operation: str,
        cost_micro_usd: int | None = None,
        record_count: int | None = None,
        latency_ms: int | None = None,
        account_id: str | None = None,
        account_label: str | None = None,
    ) -> None:
        """Record a vector-DB call so the span lands in the gateway's ``vector`` cost-layer bucket.

        Bucketing is keyed off ``gen_ai.operation.name == 'vector'``. The call's cost rides on
        ``gen_ai.tool.cost_micro_usd`` (same carrier as ``record_tool_call`` — the gateway promotes
        it into the layer's spend). The tool-name slot encodes ``{provider}.{index}.{operation}``.
        When ``cost_micro_usd`` is omitted we resolve the rate from the versioned price catalog
        (CTO-141) under ``PriceType.VECTOR_CALL`` keyed by ``(provider, operation)`` and stamp
        ``price_catalog_version`` on the span; an unknown pair (or no catalog) defaults to 0 with a
        one-time WARN. A caller-supplied ``cost_micro_usd`` always overrides. Never raises.

        ``record_count`` and ``latency_ms`` are accepted for API symmetry with future span fields
        but are not yet emitted as schema attributes (no conformant key exists for them).

        ``account_id`` tags the span with the tenant's own customer (CTO-181). Omit it and the
        account set by :func:`~tally.context.with_account` for the surrounding scope applies;
        pass it to override that for this one call. It is hashed with the tenant's HMAC key and
        never travels raw. ``account_label`` is optional, wire-only, and only carried when a
        hash was produced.
        """

        @safe(self.obs, where="TallyClient.record_vector_call")
        def _do() -> None:
            ctx = current_context()
            if ctx.trace_id is None:
                note_context_drop(self.obs, where="record_vector_call")

            resolved_cost = cost_micro_usd
            catalog_version: str | None = None
            if resolved_cost is None:
                key = (provider, operation)
                if self.catalog is not None:
                    resolved_cost, version = compute_call_cost_micro_usd(
                        self.catalog,
                        provider,
                        operation,
                        PriceType.VECTOR_CALL,
                        tenant_id=self.tenant_id,
                    )
                    catalog_version = version or None
                else:
                    resolved_cost = 0
                if not catalog_version:
                    resolved_cost = 0
                    if key not in _warned_vector_pairs:
                        _warned_vector_pairs.add(key)
                        _log.warning(
                            "no catalog vector price for (%s, %s); defaulting cost to 0",
                            provider,
                            operation,
                        )

            acct_hash, acct_version, acct_label = self._resolve_account(account_id, account_label)
            fields = SpanFields(
                system=provider,
                operation="vector",
                tool_name=f"{provider}.{index}.{operation}",
                tool_cost_micro_usd=resolved_cost,
                price_catalog_version=catalog_version,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
                account_id_hash=acct_hash,
                account_id_hash_key_version=acct_version,
                account_label=acct_label,
            )
            self._emit(build_span_attributes(fields))

        _do()

    # --- guardrails (may raise, by design — pre-call check) ---
    def guard(self, state: GuardrailState, config: GuardrailConfig) -> Verdict:
        """Consult guardrails before the next call. May raise CostLimitExceededException in
        GRACEFUL/HARD_STOP modes — that propagation is intentional (the agent framework catches it
        and degrades). Not wrapped in the safety boundary."""
        return self.guardrails.evaluate(state, config)

    # --- account dimension (CTO-181) -------------------------------------------------------
    def _resolve_account(
        self,
        account_id: str | None,
        account_label: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve ``(hash, key_version, label)`` for a call. Never raises.

        Precedence is per-call override, then the context set by
        :func:`~tally.context.with_account`. That ordering is the whole point of the API shape:
        a web app resolves the customer once per request and every span inside inherits it, while
        one call that genuinely belongs to a different account says so inline.

        The raw id is HMAC'd here, at the last possible moment, and discarded. If we cannot hash
        it (no tenant, no registry, or the tenant has no provisioned key) we emit the span with no
        account rather than dropping it or, worse, putting the raw id on the wire. An
        unattributed span is honest; a raw customer id is a leak.
        """
        ctx = current_context()
        raw = account_id if account_id is not None else ctx.account_id
        label = account_label if account_label is not None else ctx.account_label
        if not raw:
            return None, None, None

        if self.hmac_registry is None or not self.tenant_id:
            self._warn_account_once(
                "account_id supplied but no %s configured; span emitted unattributed",
                "hmac_registry" if self.hmac_registry is None else "tenant_id",
            )
            return None, None, None

        try:
            stamped = self.hmac_registry.hash_account(self.tenant_id, raw)
        except (KeyError, ValueError) as exc:
            self._warn_account_once(
                "could not hash account_id (%s); span emitted unattributed", exc
            )
            return None, None, None

        # The label only means anything alongside a hash, since it is keyed on one in the
        # gateway's label store, so it never travels alone.
        return stamped.value, stamped.key_version, (label or None)

    def _warn_account_once(self, msg: str, *args: object) -> None:
        if self.tenant_id in _warned_account_tenants:
            return
        _warned_account_tenants.add(self.tenant_id)
        _log.warning(msg, *args)

    def _emit(self, attributes: dict[str, object]) -> None:
        if self._processor is not None:
            self._processor.enqueue(attributes)
        else:
            self._exporter.export(attributes)
