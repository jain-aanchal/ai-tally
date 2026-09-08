# SPDX-License-Identifier: Apache-2.0
"""Server-side cost enrichment.

Implements CTO-35. Spec §12.3.

Cost must be trustworthy and consistent, so the gateway recomputes it from the price catalog
rather than trusting the client. The client-emitted cost is kept only as a *hint*: if it diverges
from the server value by more than a threshold (default 5%), that's logged as catalog drift (the
client's price table is stale, or ours is). The authoritative server value is written onto the
span along with the catalog version (so it can be recomputed later).

Pure function over a span attribute dict, no infra.

Non-LLM layers (tools, CTO-135; vector, CTO-142) are priced per *call*, not per token, and the SDK
carries their cost on ``gen_ai.tool.cost_micro_usd``. They go through the same funnel here so that
``gen_ai.cost.estimated_micro_usd`` stays the one canonical cost attribute every consumer reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tally.pricing import (
    PriceCatalog,
    PriceType,
    Usage,
    compute_call_cost_micro_usd,
    compute_cost_micro_usd,
    compute_embedding_cost_micro_usd,
)
from tally.schema import GenAI

# ``gen_ai.operation.name`` values priced under ``PriceType.EMBEDDING`` rather than INPUT/OUTPUT.
_EMBEDDING_OPERATIONS = frozenset({"embeddings", "embedding"})

DEFAULT_DRIFT_THRESHOLD = 0.05  # 5%

# Operations whose spend is a flat per-call price instead of token math. Keyed on
# ``gen_ai.operation.name``, the same discriminator the cost-layer buckets use.
_CALL_PRICED_OPERATIONS: dict[str, PriceType] = {
    "tool": PriceType.TOOL_CALL,
    "vector": PriceType.VECTOR_CALL,
}


# CTO-244: which token counts an operation has to carry before its cost can be resolved. The rule
# differs per operation because the layers are BILLED differently, not as a convenience, so do not
# collapse these back into one both-sides check:
#   - an embedding call has no output side at all by design, and is priced off the input tokens
#     alone (see compute_embedding_cost_micro_usd), so demanding an output count marks every
#     correctly instrumented embedding span unknown-usage and refuses to price it;
#   - tool / vector / compute / egress calls are priced PER CALL and carry no token counts at all,
#     so there is no token usage for them to be unknown about.
# These sets are the write-side half of one rule: they mirror, operation for operation, the
# UnknownUsageSpanCount predicate in db/clickhouse/rollups.sql. Change one and you must change the
# other, or a span can be counted unknown-usage on read while carrying a priced cost from write.
_INPUT_ONLY_OPERATIONS = frozenset({"embeddings"})
_PER_CALL_OPERATIONS = frozenset({"tool", "vector", "compute", "egress"})


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    attributes: dict[str, object]
    server_cost_micro_usd: int | None
    client_cost_micro_usd: int | None
    drift: float | None
    drift_exceeded: bool
    catalog_miss: bool
    # CTO-244: distinct from catalog_miss. The catalog COULD price this model; we simply were not
    # told enough usage to do it. Both land NULL / 'unpriced', but they are different diagnoses and
    # the rollups count them separately.
    usage_unknown: bool = False


def _int_or_none(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _usage_or_none(attributes: dict[str, object]) -> Usage | None:
    """Build a :class:`Usage` only when the producer actually reported token counts.

    CTO-244. ``compute_cost_micro_usd`` prices whatever it is handed, so coercing an absent or
    unparseable count to 0 here produced a confident ``EstimatedCost = 0`` with
    ``CostSource = 'estimated'`` for a real, billed call: a fabricated number asserted as priced.
    That is the exact failure the Nullable cost columns exist to prevent. Returning None instead
    leaves the cost unresolved so the row lands NULL with ``CostSource = 'unpriced'``.

    What counts as "actually reported" is per operation, keyed off ``gen_ai.operation.name``: the
    same discriminator the cost layers and the rollup's UnknownUsageSpanCount predicate use. See
    ``_INPUT_ONLY_OPERATIONS`` / ``_PER_CALL_OPERATIONS`` for why the rule cannot be one both-sides
    check. Chat is the fallback for an absent or unrecognised operation, because a token-priced LLM
    call understated by a missing side is the failure worth being strict about.

    The invariant both the read and write sides keep is that a span counted as unknown-usage must
    not also carry a priced cost. A provider that genuinely reports 0 is reporting a number, so it
    still prices as a real 0. Cached input is a refinement of a known input count rather than a
    tier of its own, so an absent one bills the full input rate, which is what the catalog does
    when no cached rate is seeded.
    """
    operation = attributes.get(GenAI.OPERATION_NAME)
    op = operation.strip().lower() if isinstance(operation, str) else ""
    cached_tokens = _int_or_none(attributes.get(GenAI.USAGE_CACHED_INPUT_TOKENS)) or 0

    if op in _PER_CALL_OPERATIONS:
        # Priced per call from the catalog, so there are no counts to miss. Any per-call branch
        # upstream of here resolves the cost before this point; this only keeps the token check
        # from vetoing an operation that never had tokens.
        return Usage(input_tokens=0, output_tokens=0, cached_input_tokens=0)

    input_tokens = _int_or_none(attributes.get(GenAI.USAGE_INPUT_TOKENS))
    output_tokens = _int_or_none(attributes.get(GenAI.USAGE_OUTPUT_TOKENS))

    if op in _INPUT_ONLY_OPERATIONS:
        if input_tokens is None:
            return None
        # An absent output side is the norm here, not an omission, so it is a real 0 rather than
        # an unknown. The input count is what the embedding rate is applied to.
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens if output_tokens is not None else 0,
            cached_input_tokens=cached_tokens,
        )

    if input_tokens is None or output_tokens is None:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
    )


def enrich_cost(
    attributes: dict[str, object],
    catalog: PriceCatalog,
    *,
    at: date | None = None,
    tenant_id: str | None = None,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> EnrichmentResult:
    """Recompute cost server-side and write the authoritative value onto a copy of ``attributes``.

    - server value (from the catalog) overwrites ``gen_ai.cost.estimated_micro_usd``;
    - ``gen_ai.cost.price_catalog_version`` is set;
    - the client-emitted value is treated as a hint and compared for drift;
    - on a catalog miss the cost key is removed and ``catalog_miss`` is True (span still returned);
    - CTO-244: when the model is priceable but no usable token counts were reported the cost key
      is likewise removed and ``usage_unknown`` is True, so the row lands NULL / 'unpriced'
      rather than claiming a priced 0.

    Tool and vector spans (``gen_ai.operation.name`` of ``tool``/``vector``) are priced per call
    and take the branch in :func:`_enrich_call_cost`; everything else is priced from token usage.
    """
    out = dict(attributes)

    operation = out.get(GenAI.OPERATION_NAME)
    price_type = _CALL_PRICED_OPERATIONS.get(operation) if isinstance(operation, str) else None
    if price_type is not None:
        return _enrich_call_cost(
            out, catalog, price_type, at=at, tenant_id=tenant_id, drift_threshold=drift_threshold
        )

    client_cost = _int_or_none(out.get(GenAI.COST_ESTIMATED_MICRO_USD))

    provider = out.get(GenAI.SYSTEM)
    model = out.get(GenAI.RESPONSE_MODEL) or out.get(GenAI.REQUEST_MODEL)
    if not isinstance(provider, str) or not isinstance(model, str):
        # nothing to price against
        return EnrichmentResult(out, None, client_cost, None, False, catalog_miss=True)

    # CTO-243: an embeddings span is priced under PriceType.EMBEDDING, not INPUT/OUTPUT. Routing it
    # through compute_cost_micro_usd (which only reads the INPUT/OUTPUT tiers) resolved no rate at
    # all, so every embeddings span reported a catalog miss and had its cost dropped, landing 0.00
    # in EstimatedCost. That is the fabricated-zero the honesty invariant forbids: the seed catalog
    # DOES price text-embedding-3-*, and the SDK's own record_embedding_call already computes the
    # right number client-side (tally.client, "use the embedding-specific resolver") only for the
    # gateway to overwrite it with nothing. Use the same resolver here so the authoritative
    # server-side value is the catalog value. A genuinely unpriced embedding model still misses and
    # still renders blank rather than 0.
    operation = out.get(GenAI.OPERATION_NAME)
    is_embedding = isinstance(operation, str) and operation.lower() in _EMBEDDING_OPERATIONS

    usage = _usage_or_none(out)
    if usage is None:
        # CTO-244: priceable model, unknown usage. Assert nothing so the row lands NULL/'unpriced'.
        out.pop(GenAI.COST_ESTIMATED_MICRO_USD, None)
        out.pop(GenAI.COST_PRICE_CATALOG_VERSION, None)
        return EnrichmentResult(
            out, None, client_cost, None, False, catalog_miss=False, usage_unknown=True
        )

    if is_embedding:
        server_cost, version = compute_embedding_cost_micro_usd(
            catalog, provider, model, usage.input_tokens, at=at, tenant_id=tenant_id
        )
    else:
        server_cost, version = compute_cost_micro_usd(
            catalog, provider, model, usage, at=at, tenant_id=tenant_id
        )

    catalog_miss = not version
    if catalog_miss:
        # no authoritative price → don't assert a cost
        out.pop(GenAI.COST_ESTIMATED_MICRO_USD, None)
        out.pop(GenAI.COST_PRICE_CATALOG_VERSION, None)
        return EnrichmentResult(out, None, client_cost, None, False, catalog_miss=True)

    # authoritative server value wins
    out[GenAI.COST_ESTIMATED_MICRO_USD] = server_cost
    out[GenAI.COST_CURRENCY] = out.get(GenAI.COST_CURRENCY, "USD")
    out[GenAI.COST_PRICE_CATALOG_VERSION] = version

    drift: float | None = None
    drift_exceeded = False
    if client_cost is not None and server_cost > 0:
        drift = abs(client_cost - server_cost) / server_cost
        drift_exceeded = drift > drift_threshold

    return EnrichmentResult(
        attributes=out,
        server_cost_micro_usd=server_cost,
        client_cost_micro_usd=client_cost,
        drift=drift,
        drift_exceeded=drift_exceeded,
        catalog_miss=False,
    )


def _catalog_name(operation: str, tool_name: str) -> str:
    """Return the catalog ``model`` slot for a per-call priced span.

    Tool spans put the tool name straight in ``gen_ai.tool.name`` and the catalog is keyed on it.
    Vector spans encode ``{provider}.{index}.{operation}`` in the same slot (CTO-142) because the
    index is worth keeping on the span, while the catalog prices ``(provider, operation)``; the
    operation is the last dot-segment, so an index name containing dots still resolves.
    """
    if operation == "vector":
        return tool_name.rsplit(".", 1)[-1]
    return tool_name


def _enrich_call_cost(
    out: dict[str, object],
    catalog: PriceCatalog,
    price_type: PriceType,
    *,
    at: date | None,
    tenant_id: str | None,
    drift_threshold: float,
) -> EnrichmentResult:
    """Resolve the cost of a per-call priced span (tool, vector) onto the canonical cost key.

    The SDK writes these costs to ``gen_ai.tool.cost_micro_usd``, which is a carrier attribute and
    not a cost column: until CTO-243 nothing promoted it, so every tool and vector span landed in
    ClickHouse with EstimatedCost 0 and a whole layer of real spend read as free. The promotion
    happens here, on the one path that already owns cost, rather than by teaching the row mapper a
    second cost field.

    Precedence matches the LLM path: the server catalog is authoritative and the client value is a
    drift hint. The one deliberate difference is the miss case. A per-call price is frequently a
    negotiated rate the catalog cannot know, so when the catalog has no entry we keep the
    client-reported figure instead of discarding it; discarding it is what made the spend
    invisible. With no entry and no client figure there is nothing honest to assert, so the cost
    key is dropped exactly as on the LLM path.
    """
    client_cost = _int_or_none(out.get(GenAI.TOOL_COST_MICRO_USD))

    provider = out.get(GenAI.SYSTEM)
    tool_name = out.get(GenAI.TOOL_NAME)
    operation = out.get(GenAI.OPERATION_NAME)
    server_cost = 0
    version = ""
    if isinstance(provider, str) and isinstance(tool_name, str) and isinstance(operation, str):
        server_cost, version = compute_call_cost_micro_usd(
            catalog,
            provider,
            _catalog_name(operation, tool_name),
            price_type,
            at=at,
            tenant_id=tenant_id,
        )

    if not version:
        if client_cost is None:
            # Nothing priced it and the client asserted nothing: do not invent a number.
            out.pop(GenAI.COST_ESTIMATED_MICRO_USD, None)
            out.pop(GenAI.COST_PRICE_CATALOG_VERSION, None)
            return EnrichmentResult(out, None, None, None, False, catalog_miss=True)
        # Client-reported spend survives. The catalog version the SDK stamped (if any) rides along
        # untouched so the number keeps its provenance.
        out[GenAI.COST_ESTIMATED_MICRO_USD] = client_cost
        out[GenAI.COST_CURRENCY] = out.get(GenAI.COST_CURRENCY, "USD")
        return EnrichmentResult(out, None, client_cost, None, False, catalog_miss=True)

    out[GenAI.COST_ESTIMATED_MICRO_USD] = server_cost
    out[GenAI.COST_CURRENCY] = out.get(GenAI.COST_CURRENCY, "USD")
    out[GenAI.COST_PRICE_CATALOG_VERSION] = version

    drift: float | None = None
    drift_exceeded = False
    if client_cost is not None and server_cost > 0:
        drift = abs(client_cost - server_cost) / server_cost
        drift_exceeded = drift > drift_threshold

    return EnrichmentResult(
        attributes=out,
        server_cost_micro_usd=server_cost,
        client_cost_micro_usd=client_cost,
        drift=drift,
        drift_exceeded=drift_exceeded,
        catalog_miss=False,
    )
