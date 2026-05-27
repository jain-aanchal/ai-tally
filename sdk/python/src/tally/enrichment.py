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


def _int_or_none(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


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
    - on a catalog miss the cost key is removed and ``catalog_miss`` is True (span still returned).
    """
    out = dict(attributes)
    client_cost = _int_or_none(out.get(GenAI.COST_ESTIMATED_MICRO_USD))

    provider = out.get(GenAI.SYSTEM)
    model = out.get(GenAI.RESPONSE_MODEL) or out.get(GenAI.REQUEST_MODEL)
    if not isinstance(provider, str) or not isinstance(model, str):
        # nothing to price against
        return EnrichmentResult(out, None, client_cost, None, False, catalog_miss=True)

    usage = Usage(
        input_tokens=_int_or_none(out.get(GenAI.USAGE_INPUT_TOKENS)) or 0,
        output_tokens=_int_or_none(out.get(GenAI.USAGE_OUTPUT_TOKENS)) or 0,
        cached_input_tokens=_int_or_none(out.get(GenAI.USAGE_CACHED_INPUT_TOKENS)) or 0,
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
