# SPDX-License-Identifier: Apache-2.0
"""TallyClient: the SDK entrypoint.

Ties the spine together: schema (CTO-47) + safety (CTO-45) + context (CTO-46) + sampling (CTO-50)
+ pricing (CTO-52) + egress (CTO-49), with a cohesive high-level ``record_llm_call()`` API.

Every public method runs inside the safety boundary so a bug in the SDK, or a pluggable
exporter/transport, never escapes into the customer's code path. Guardrail *enforcement* is the
one intentional exception and lives behind :meth:`guard` (it may raise, by design, for the agent
framework to catch); ``record_llm_call`` itself never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from tally.context import current_context, new_trace_id, note_synthetic_trace
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
from tally.schema import (
    BILLING_MODE_API,
    BILLING_MODE_SUBSCRIPTION,
    SPAN_ID_KEY,
    TIMESTAMP_NS_KEY,
    TRACE_ID_KEY,
    TRACE_ID_SYNTHETIC_KEY,
    SpanFields,
    build_span_attributes,
    new_span_id,
)

_log = logging.getLogger("tally")

# Tool + vector per-call prices now live in the versioned price catalog (CTO-141) under
# PriceType.TOOL_CALL / PriceType.VECTOR_CALL; the inline ``_TOOL_PRICING`` / ``_VECTOR_PRICING``
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
    #: The span that was emitted, ids and timestamp included, as a COPY: mutating it never reaches
    #: the span on its way to storage (CTO-404).
    #:
    #: The shape deliberately differs between a kept and a sampled-out call. When ``kept`` is True
    #: this names the stored span. When it is False no span exists, so this is the pre-stamp
    #: attribute set with no ``trace_id``/``span_id``/``timestamp_ns`` in it, and ``trace_id``
    #: above is the context's own (``None`` when there is none). Inventing ids for a span that was
    #: never sent would be the dishonest direction, so branch on ``kept``, not on key presence.
    attributes: dict[str, object]


@dataclass(frozen=True, slots=True)
class EmbeddingCallResult:
    """Result of :meth:`TallyClient.record_embedding_call` (CTO-136).

    Mirrors :class:`LlmCallResult` but without sampling fields; embeddings always emit.
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
        billing_mode: default ``gen_ai.cost.billing_mode`` for the catalog-priced calls this client
            records (CTO-417), one of ``tally.schema.BILLING_MODES``. Leave it None and spans carry
            no mode, which prices them from the catalog exactly as before. Set it to
            ``"subscription"`` for a process whose LLM traffic runs entirely under a seat or plan.
            Per-call arguments override it, which is the case that matters: the motivating tenant
            mixes subscription and per-token API traffic inside one tenant (their Fireworks spend is
            real per-token API spend while their openai and claude-code traffic is not), so a
            process-wide switch alone would be wrong for one half of it either way round.
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
        billing_mode: str | None = None,
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
        self.billing_mode = billing_mode
        self.hmac_registry = hmac_registry

    def _resolve_billing_mode(self, billing_mode: str | None) -> str | None:
        """Per-call mode, else the client default, else None (CTO-417).

        None is returned rather than ``"api"`` so the attribute is simply absent on a span nobody
        declared a mode for. Absence and ``"api"`` price identically, and emitting nothing keeps
        pre-CTO-417 spans byte-identical on the wire, which is what makes the old-producer
        behaviour testable rather than merely asserted.
        """
        mode = billing_mode if billing_mode is not None else self.billing_mode
        if mode is None:
            return None
        normalised = mode.strip().lower()
        if normalised not in (BILLING_MODE_API, BILLING_MODE_SUBSCRIPTION):
            # Warn rather than raise: the SDK never takes a customer's process down over telemetry.
            # The gateway refuses to price what it cannot read, so the span stays honest either way.
            _log.warning(
                "unknown billing mode %r; span will be sent unpriced rather than at list rates",
                mode,
            )
        return normalised

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
        billing_mode: str | None = None,
    ) -> LlmCallResult:
        """Record an LLM call end-to-end. Never raises.

        ``provider`` is a free-form string used verbatim as ``gen_ai.system`` and as the
        catalog lookup key; there is no provider allowlist, so any provider the catalog
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

        ``billing_mode`` declares HOW this one call was billed (CTO-417), overriding the client-wide
        default. Pass ``"subscription"`` when the call is covered by a seat or plan: no cost is then
        estimated here and none is assigned server-side, because there is no per-call price to know.
        Omit it and the call prices from the catalog exactly as before. This is per call because a
        single process routinely mixes the two, and it is CLIENT-ASSERTED: nothing downstream can
        corroborate it, so it must not be used as a billing control (see
        ``tally.schema.BILLING_MODES`` and CTO-410).
        """

        @safe(self.obs, where="TallyClient.record_llm_call", fallback=None)
        def _do() -> LlmCallResult:
            ctx = current_context()
            trace_id = ctx.trace_id
            if trace_id is None:
                note_synthetic_trace(self.obs, where="record_llm_call")

            # Billing counts at HEAD, before sampling (CTO-50/CTO-84).
            if trace_id is not None:
                self.billing.count_trace(trace_id)

            # CTO-417: resolved before the estimate, because a subscription-billed call must not be
            # priced client-side either. Leaving the local estimate in place would put a list-rate
            # figure on LlmCallResult.cost_micro_usd and into the customer's own dashboards, and
            # would hand the gateway a client cost hint for a call that has no price.
            mode = self._resolve_billing_mode(billing_mode)
            priced_per_token = mode != BILLING_MODE_SUBSCRIPTION

            cost_micro: int | None = None
            catalog_version: str | None = None
            if self.catalog is not None and priced_per_token:
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
                billing_mode=mode,
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
            # attribute, so the span stays schema-conformant. It's returned in the result.
            if decision.keep:
                emitted = self._emit(attrs)
                # The result describes the span that was actually SENT (CTO-404). The ids are
                # stamped on a copy, so this used to hand back a trace_id of None and an attribute
                # dict with no ids in it, for a span the gateway stored under a real synthetic
                # trace id: a customer logging result.trace_id to correlate with the dashboard was
                # given a value that matches nothing there.
                result_trace = str(emitted[TRACE_ID_KEY])
                # A COPY, deliberately. ``_emit`` returns the dict it enqueued, so handing that
                # object to the caller would let a customer's own bookkeeping
                # (``result.attributes["my.note"] = prompt``) write into a span that is already
                # queued for export and already past SDK-side validation. A body-shaped string
                # would then ride the long-tail attribute map into storage, which is the
                # no-bodies-in-telemetry invariant reachable through a public API (CTO-404).
                result_attrs = dict(emitted)
            else:
                # Sampled out, so no span exists to describe. Reporting the context's own trace id
                # (None when there is none) stays honest rather than inventing an id for a span
                # that was never sent.
                result_trace, result_attrs = trace_id, attrs

            return LlmCallResult(
                trace_id=result_trace,
                cost_micro_usd=cost_micro,
                kept=decision.keep,
                sample_rate=decision.sample_rate,
                attributes=result_attrs,
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
        ``gen_ai.tool.cost_micro_usd``; the gateway promotes it into the span's canonical cost
        (``gen_ai.cost.estimated_micro_usd``, the ``EstimatedCost`` column) during enrichment,
        preferring its own catalog price where it has one (CTO-243).
        When ``cost_micro_usd`` is omitted we resolve the rate from
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
                note_synthetic_trace(self.obs, where="record_tool_call")

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
        billing_mode: str | None = None,
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

        ``billing_mode`` works exactly as on :meth:`record_llm_call` (CTO-417): an embedding covered
        by a subscription is estimated at nothing here and assigned nothing server-side.
        """

        @safe(self.obs, where="TallyClient.record_embedding_call", fallback=None)
        def _do() -> EmbeddingCallResult:
            ctx = current_context()
            trace_id = ctx.trace_id
            if trace_id is None:
                note_synthetic_trace(self.obs, where="record_embedding_call")

            # CTO-417: same ordering and same reason as record_llm_call.
            mode = self._resolve_billing_mode(billing_mode)
            priced_per_token = mode != BILLING_MODE_SUBSCRIPTION

            cost_micro: int | None = None
            catalog_version: str | None = None
            if self.catalog is not None and priced_per_token:
                # Embeddings are priced under PriceType.EMBEDDING, not INPUT; use the
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
                billing_mode=mode,
                feature_tag=ctx.feature_tag,
                session_id=ctx.session_id,
                account_id_hash=acct_hash,
                account_id_hash_key_version=acct_version,
                account_label=acct_label,
            )
            attrs = build_span_attributes(fields)
            emitted = self._emit(attrs)
            # Same correction as record_llm_call: the result names the span that was sent, ids
            # included, rather than the pre-stamp copy (CTO-404). Embeddings always emit, so there
            # is no sampled-out branch here.
            return EmbeddingCallResult(
                trace_id=str(emitted[TRACE_ID_KEY]),
                cost_micro_usd=cost_micro,
                # A copy, for the reason spelled out in record_llm_call (CTO-404).
                attributes=dict(emitted),
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
        ``gen_ai.tool.cost_micro_usd`` (same carrier as ``record_tool_call``, and the gateway
        promotes it into the span's canonical cost during enrichment, CTO-243). The tool-name slot
        encodes ``{provider}.{index}.{operation}``, and the gateway prices off the last segment.
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
                note_synthetic_trace(self.obs, where="record_vector_call")

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

    # --- guardrails (may raise, by design; pre-call check) ---
    def guard(self, state: GuardrailState, config: GuardrailConfig) -> Verdict:
        """Consult guardrails before the next call. May raise CostLimitExceededException in
        GRACEFUL/HARD_STOP modes; that propagation is intentional (the agent framework catches it
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

    def _emit(self, attributes: dict[str, object]) -> dict[str, object]:
        """Stamp the span's identity and emit it. Returns the span that was actually sent.

        Returning it is what lets a caller-facing result describe what LANDED rather than what was
        built: the stamping happens on a copy, so before CTO-404 the result and the stored span
        disagreed about both ids and the timestamp.

        The returned dict IS the enqueued span, not a copy of it. Anything that hands it onward to
        customer code has to copy it first, or the customer can mutate a span that is already
        queued and already validated.
        """
        span = _with_span_ids(attributes)
        if self._processor is not None:
            self._processor.enqueue(span)
        else:
            self._exporter.export(span)
        return span


def _with_span_ids(attributes: dict[str, object]) -> dict[str, object]:
    """Return the span with its structural ``(trace_id, span_id)`` pair filled in (CTO-396).

    Every span leaving this SDK needs its own identity, because the ingest contract is keyed on one:
    the wire envelope dedupes spans within a batch on ``(trace_id, span_id)`` and the write path
    keys the ClickHouse row on the same pair. Emitting none of it made every span in a batch
    indistinguishable, so all but the first were discarded as duplicates and a customer's spend
    disappeared without an error anywhere. The edge proxy has always sent both fields, which is why
    only SDK-metered traffic was affected.

    The trace id is the one the caller's context already tracks, so spans of one trace stay joined.
    A span emitted with no active trace still gets a fresh trace id and its own span id rather than
    nothing: unattributed to a trace is honest, sharing an id with every other trace-less span is
    not. Both ids are random and encode nothing about the request or the customer.

    The span's own ``timestamp_ns`` is stamped here too, and for the same reason (CTO-404). A span
    that carries no timestamp inherits the ENVELOPE's ``client_send_ts_ns`` at the gateway, and
    that is a different number in every envelope. So the same spans re-enveloped in a NEW
    ``BatchRequest`` carried identical ids but a different time, which is part of the ClickHouse
    sorting key: the ReplacingMergeTree then had two rows it could never collapse, and the spend
    was counted twice.
    Stamping it at emit makes a span fully self-describing, so a re-send is genuinely the same row.

    Ids and timestamp the caller already set (either spelling) are left alone: an OTel-shaped
    producer feeding ``ingest_span`` owns its identity and its clock, and we must not overwrite
    either. A copy is returned so the dict the caller passed in is never mutated behind its back.
    That copy is the span that gets enqueued, though, so whoever passes it on to customer code has
    to copy it again: see ``_emit`` and ``LlmCallResult.attributes``.
    """
    has_trace = bool(attributes.get(TRACE_ID_KEY) or attributes.get("TraceId"))
    has_span = bool(attributes.get(SPAN_ID_KEY) or attributes.get("SpanId"))
    # Not a truthiness test: a timestamp of 0 is a real (if improbable) value, and re-stamping it
    # would be exactly the overwrite the paragraph above forbids.
    has_ts = (
        attributes.get(TIMESTAMP_NS_KEY) is not None or attributes.get("Timestamp") is not None
    )
    if has_trace and has_span and has_ts:
        return attributes
    span = dict(attributes)
    if not has_trace:
        ctx_trace = current_context().trace_id
        if ctx_trace is None:
            # CTO-401: a minted trace id is marked as minted. The id itself is indistinguishable
            # from a real one by construction (both are random hex), so without this the gateway
            # head meter has no way to tell "the customer started a trace" from "we invented an id
            # so this span would have an identity", and it counted one billable trace per
            # trace-less span. The id still travels, so the stored row and the ClickHouse-derived
            # invoice count are exactly what CTO-396 made them.
            span[TRACE_ID_KEY] = new_trace_id()
            span[TRACE_ID_SYNTHETIC_KEY] = True
        else:
            span[TRACE_ID_KEY] = ctx_trace
    if not has_span:
        span[SPAN_ID_KEY] = new_span_id()
    if not has_ts:
        span[TIMESTAMP_NS_KEY] = time.time_ns()
    return span
