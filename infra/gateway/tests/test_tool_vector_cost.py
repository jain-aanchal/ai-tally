# SPDX-License-Identifier: Apache-2.0
"""CTO-243: tool and vector spend must reach the EstimatedCost column, not read as $0.

The SDK writes per-call spend to ``gen_ai.tool.cost_micro_usd``. Nothing promoted that carrier into
the canonical cost attribute, so every tool and vector span was written with EstimatedCost 0 and a
whole cost layer looked free. These tests drive the real ingest path (POST /v1/batches into a fake
ClickHouse) and assert the row that actually gets written.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from fastapi.testclient import TestClient
from tally.schema import GenAI, SpanFields, build_span_attributes

from gateway import app as app_module
from gateway.app import app
from gateway.config import get_settings
from gateway.mapping import COLUMNS


class FakeStore:
    def __init__(self) -> None:
        self.spans: list[tuple] = []
        self._lock = threading.Lock()

    def insert_spans(self, rows: list[tuple]) -> int:
        with self._lock:
            self.spans.extend(rows)
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@contextmanager
def _client(store: FakeStore) -> Iterator[TestClient]:
    """A TestClient writing synchronously into ``store`` (buffering off, so rows land on POST)."""
    settings = get_settings()
    prev_buffered = settings.ingest_buffered
    settings.ingest_buffered = False
    orig_factory = app_module.ClickHouseStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    try:
        with TestClient(app) as c:
            app.state.settings.require_api_key = False
            yield c
    finally:
        app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
        settings.ingest_buffered = prev_buffered


def _post(c: TestClient, spans: list[dict]) -> dict:
    r = c.post(
        "/v1/batches",
        json={"tenant_id": "t-local", "sdk_version": "test", "resource_spans": spans},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _row(store: FakeStore, index: int = 0) -> dict[str, object]:
    return dict(zip(COLUMNS, store.spans[index], strict=True))


def _tool_span(**over: object) -> dict:
    attrs = build_span_attributes(
        SpanFields(
            system="tavily",
            operation="tool",
            tool_name="search",
            tool_call_id="call-1",
            tool_cost_micro_usd=10_000,  # 0.01 USD, what record_tool_call resolves
            feature_tag="assistant",
        )
    )
    attrs.update({"trace_id": "trace-tool", "span_id": "span-tool"})
    attrs.update(over)
    return attrs


def _vector_span(**over: object) -> dict:
    attrs = build_span_attributes(
        SpanFields(
            system="pinecone",
            operation="vector",
            tool_name="pinecone.docs.query",
            tool_cost_micro_usd=400,  # 0.0004 USD
            feature_tag="assistant",
        )
    )
    attrs.update({"trace_id": "trace-vec", "span_id": "span-vec"})
    attrs.update(over)
    return attrs


def test_recorded_tool_call_no_longer_reads_zero() -> None:
    """Regression for the reported symptom: a recorded tool call showing $0 in the product."""
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_tool_span()])

    row = _row(store)
    assert row["GenAiOperation"] == "tool"
    assert row["EstimatedCost"] != Decimal(0)
    assert row["EstimatedCost"] == Decimal("0.01")


def test_vector_call_cost_lands_on_the_row() -> None:
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_vector_span()])

    row = _row(store)
    assert row["GenAiOperation"] == "vector"
    # Priced off the last segment of "pinecone.docs.query", the operation the catalog keys on.
    assert row["EstimatedCost"] == Decimal("0.0004")
    assert row["PriceCatalogVersion"] != ""


def test_client_reported_cost_survives_a_catalog_miss() -> None:
    """A negotiated per-call rate the catalog cannot know must not be discarded as $0."""
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_tool_span(**{GenAI.SYSTEM: "acme-internal", GenAI.TOOL_NAME: "lookup"})])

    row = _row(store)
    assert row["EstimatedCost"] == Decimal("0.01")  # the client figure, kept
    assert row["PriceCatalogVersion"] == ""  # no catalog priced it, so no version is claimed


def test_unpriced_tool_span_is_not_given_a_fabricated_cost() -> None:
    """No catalog entry and no client figure: nothing is asserted beyond the base column default."""
    store = FakeStore()
    span = _tool_span(**{GenAI.SYSTEM: "acme-internal", GenAI.TOOL_NAME: "lookup"})
    span.pop(GenAI.TOOL_COST_MICRO_USD)
    with _client(store) as c:
        _post(c, [span])

    row = _row(store)
    assert row["PriceCatalogVersion"] == ""
    # Nullable cost (CTO-244) has landed, so the absent cost is no longer coerced to Decimal(0):
    # it reaches storage as NULL, tagged unpriced, which is the outcome this test always wanted.
    # The earlier Decimal(0) expectation recorded the mapper's coercion, not the intended contract.
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "unpriced"


def test_llm_span_cost_is_unchanged() -> None:
    """The token-priced path must be untouched by the per-call branch."""
    store = FakeStore()
    attrs = build_span_attributes(
        SpanFields(
            system="openai",
            request_model="gpt-5-mini",
            response_model="gpt-5-mini",
            operation="chat",
            input_tokens=1_000_000,
            output_tokens=0,
        )
    )
    attrs.update({"trace_id": "trace-llm", "span_id": "span-llm"})
    with _client(store) as c:
        _post(c, [attrs])

    assert _row(store)["EstimatedCost"] > Decimal(0)
