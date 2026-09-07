# SPDX-License-Identifier: Apache-2.0
"""Server-side cost enrichment.

Implements CTO-35. Spec §12.3.

Cost must be trustworthy and consistent, so the gateway recomputes it from the price catalog
rather than trusting the client. The client-emitted cost is kept only as a *hint*: if it diverges
from the server value by more than a threshold (default 5%), that's logged as catalog drift (the
client's price table is stale, or ours is). The authoritative server value is written onto the
span along with the catalog version (so it can be recomputed later).

Pure function over a span attribute dict — no infra.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd
from tally.schema import GenAI

DEFAULT_DRIFT_THRESHOLD = 0.05  # 5%


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    attributes: dict[str, object]
    server_cost_micro_usd: int | None
    client_cost_micro_usd: int | None
    drift: float | None
    drift_exceeded: bool
    catalog_miss: bool
    # CTO-244: True when the model was priceable but the producer reported no usable token counts,
    # so no cost was resolved. Distinct from catalog_miss, which means we had no rate to apply.
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

    Input and output are both required, matching the chat branch of the rollup's
    UnknownUsageSpanCount predicate (which is per operation kind: an embedding has only an input
    side, and tool / vector spans are priced per call and have no token usage at all). The
    invariant both sides keep is that a span counted as unknown-usage must not also carry a priced
    cost. A provider that genuinely reports 0 is reporting a number, so it
    still prices as a real 0. Cached input is a refinement of a known input count rather than a
    tier of its own, so an absent one bills the full input rate, which is what the catalog does
    when no cached rate is seeded.
    """
    input_tokens = _int_or_none(attributes.get(GenAI.USAGE_INPUT_TOKENS))
    output_tokens = _int_or_none(attributes.get(GenAI.USAGE_OUTPUT_TOKENS))
    if input_tokens is None or output_tokens is None:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=_int_or_none(attributes.get(GenAI.USAGE_CACHED_INPUT_TOKENS)) or 0,
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
    - CTO-244: when the model is priceable but no usable token counts were reported the cost key is
      likewise removed and ``usage_unknown`` is True, so the row lands NULL / 'unpriced' rather
      than claiming a priced 0.
    """
    out = dict(attributes)
    client_cost = _int_or_none(out.get(GenAI.COST_ESTIMATED_MICRO_USD))

    provider = out.get(GenAI.SYSTEM)
    model = out.get(GenAI.RESPONSE_MODEL) or out.get(GenAI.REQUEST_MODEL)
    if not isinstance(provider, str) or not isinstance(model, str):
        # nothing to price against
        return EnrichmentResult(out, None, client_cost, None, False, catalog_miss=True)

    usage = _usage_or_none(out)
    if usage is None:
        # CTO-244: a known model with unknown usage is unpriced, not free. Drop the keys so the
        # span carries no cost claim at all and the row lands NULL / 'unpriced'.
        out.pop(GenAI.COST_ESTIMATED_MICRO_USD, None)
        out.pop(GenAI.COST_PRICE_CATALOG_VERSION, None)
        return EnrichmentResult(
            out, None, client_cost, None, False, catalog_miss=False, usage_unknown=True
        )

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
