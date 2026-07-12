# SPDX-License-Identifier: Apache-2.0
"""CTO-151 — Vertex AI Vector Search on the Vector cost layer.

Vertex AI Vector Search (formerly Matching Engine) is a GCP-native vector provider. It rides the
same ``record_vector_call`` path as the CTO-142 vector DBs (Pinecone/Weaviate/Qdrant): a query or
upsert emits an ``operation='vector'`` span whose per-query cost resolves from the versioned price
catalog (CTO-141) under ``PriceType.VECTOR_CALL`` keyed by ``(provider="vertex", operation)``.

Only the per-query / serving portion is priced here. Deployed-index node-hours (a compute cost)
are deferred to the GCP Cloud Billing compute connector (CTO-150), so they must NOT show up on the
Vector layer — see the cost-split comment in pricing.py.
"""

from __future__ import annotations

import logging

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.pricing import (
    PriceType,
    compute_call_cost_micro_usd,
    seed_catalog,
)
from tally.schema import GenAI, validate_span_attributes


def _client(exporter: MemoryExporter | None = None) -> TallyClient:
    return TallyClient(exporter=exporter or MemoryExporter(), catalog=seed_catalog())


def test_vertex_query_emits_vector_span_priced_from_catalog() -> None:
    exporter = MemoryExporter()
    client = _client(exporter)
    with with_trace_context(trace_id="t1", feature_tag="rag", session_id="s1"):
        client.record_vector_call(provider="vertex", index="products", operation="query")

    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    # Lands in the gateway's Vector bucket (LAYER_CASE: GenAiOperation='vector' -> 'vector').
    assert span[GenAI.OPERATION_NAME] == "vector"
    assert span[GenAI.SYSTEM] == "vertex"
    assert span[GenAI.TOOL_NAME] == "vertex.products.query"
    # Non-zero cost, resolved from the catalog (no catalog miss) -> version stamped.
    assert span[GenAI.TOOL_COST_MICRO_USD] == 400
    assert span[GenAI.COST_PRICE_CATALOG_VERSION]
    assert span[GenAI.FEATURE_TAG] == "rag"
    assert span[GenAI.SESSION_ID] == "s1"


def test_vertex_upsert_priced() -> None:
    exporter = MemoryExporter()
    client = _client(exporter)
    client.record_vector_call(provider="vertex", index="products", operation="upsert")
    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 200
    assert span[GenAI.COST_PRICE_CATALOG_VERSION]


def test_vertex_unknown_index_tier_defaults_to_zero_and_warns(caplog) -> None:
    # An unseeded Vertex operation / index tier must fail soft: 0 cost + one-time WARN, still a
    # well-formed vector span (never raises, never a partial-data crash).
    exporter = MemoryExporter()
    client = _client(exporter)
    with caplog.at_level(logging.WARNING, logger="tally"):
        client.record_vector_call(
            provider="vertex", index="products", operation="brute-force-tier"
        )

    span = exporter.spans[0]
    assert span[GenAI.TOOL_COST_MICRO_USD] == 0
    assert span[GenAI.OPERATION_NAME] == "vector"
    assert GenAI.COST_PRICE_CATALOG_VERSION not in span
    assert any("no catalog vector price" in r.message for r in caplog.records)


def test_real_seed_catalog_prices_vertex_query_and_upsert() -> None:
    # Pricing regression against the real seed_catalog(): both seeded Vertex ops price > 0.
    cat = seed_catalog()
    q_cost, q_ver = compute_call_cost_micro_usd(cat, "vertex", "query", PriceType.VECTOR_CALL)
    u_cost, u_ver = compute_call_cost_micro_usd(cat, "vertex", "upsert", PriceType.VECTOR_CALL)
    assert q_cost == 400
    assert u_cost == 200
    assert q_ver and u_ver


def test_node_hours_not_priced_on_vector_layer() -> None:
    # Node-hour / deployed-index compute is deferred to CTO-150. There must be no VECTOR_CALL
    # entry for a node-hour-style key, so it never double-counts on the Vector layer.
    cat = seed_catalog()
    cost, ver = compute_call_cost_micro_usd(cat, "vertex", "node-hour", PriceType.VECTOR_CALL)
    assert cost == 0
    assert ver == ""
