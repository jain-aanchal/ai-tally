# SPDX-License-Identifier: Apache-2.0
"""CTO-136 — record_embedding_call lands spans in the Embeddings cost-layer bucket."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.pricing import PriceCatalog, PriceEntry, PriceType, Unit, seed_catalog
from tally.schema import GenAI, validate_span_attributes

_FROM = date(2026, 5, 1)


def _embedding_catalog() -> PriceCatalog:
    # Seed under PriceType.EMBEDDING — the tier the real seed_catalog uses for
    # text-embedding-3-*. record_embedding_call resolves this tier (not INPUT).
    cat = PriceCatalog()
    cat.add(
        PriceEntry(
            version="seed-test",
            valid_from=_FROM,
            provider="openai",
            model="text-embedding-3-small",
            price_type=PriceType.EMBEDDING,
            unit=Unit.PER_MILLION_TOKENS,
            price_per_unit=Decimal("0.02"),
        )
    )
    return cat


def test_embedding_cost_matches_seeded_catalog():
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter, catalog=_embedding_catalog())
    with with_trace_context(trace_id="t1", feature_tag="rag"):
        result = client.record_embedding_call(
            provider="openai",
            model="text-embedding-3-small",
            input_tokens=1_000_000,
            at=date(2026, 6, 1),
        )

    # 1M tokens @ $0.02/Mtok = $0.02 = 20_000 micro-USD.
    assert result.cost_micro_usd == 20_000
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.OPERATION_NAME] == "embeddings"
    assert span[GenAI.SYSTEM] == "openai"
    assert span[GenAI.REQUEST_MODEL] == "text-embedding-3-small"
    assert span[GenAI.USAGE_INPUT_TOKENS] == 1_000_000
    assert span[GenAI.COST_ESTIMATED_MICRO_USD] == 20_000
    assert span[GenAI.FEATURE_TAG] == "rag"


def test_real_seed_catalog_prices_embeddings_nonzero():
    # Regression: the production seed_catalog() prices embeddings under PriceType.EMBEDDING.
    # record_embedding_call must resolve that tier — a generic INPUT-only path would cost $0.
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter, catalog=seed_catalog())
    result = client.record_embedding_call(
        provider="openai",
        model="text-embedding-3-small",
        input_tokens=1_000_000,
        at=date(2026, 6, 1),
    )
    assert result.cost_micro_usd is not None and result.cost_micro_usd > 0
    span = exporter.spans[0]
    assert span[GenAI.OPERATION_NAME] == "embeddings"
    assert span[GenAI.COST_ESTIMATED_MICRO_USD] > 0


def test_unknown_model_cost_zero_no_raise():
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter, catalog=_embedding_catalog())
    result = client.record_embedding_call(
        provider="openai", model="mystery-embed", input_tokens=500, at=date(2026, 6, 1)
    )
    # Unknown model → partial/zero price, never raises.
    assert result.cost_micro_usd == 0
    span = exporter.spans[0]
    assert span[GenAI.OPERATION_NAME] == "embeddings"
    assert GenAI.COST_ESTIMATED_MICRO_USD in span  # emitted as 0


def test_no_catalog_cost_none():
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter)  # no catalog
    result = client.record_embedding_call(
        provider="openai", model="text-embedding-3-small", input_tokens=100
    )
    assert result.cost_micro_usd is None
    span = exporter.spans[0]
    assert span[GenAI.OPERATION_NAME] == "embeddings"
    assert GenAI.COST_ESTIMATED_MICRO_USD not in span  # no cost key when None


def test_never_raises_when_exporter_throws():
    class BoomExporter:
        def export(self, attributes: dict[str, object]) -> None:
            raise RuntimeError("exporter down")

    client = TallyClient(exporter=BoomExporter(), catalog=_embedding_catalog())
    result = client.record_embedding_call(
        provider="openai", model="text-embedding-3-small", input_tokens=10
    )
    # Boundary swallowed the error and returned a benign result.
    assert result.trace_id is None and result.cost_micro_usd is None
    assert client.observability.internal_error_count == 1
