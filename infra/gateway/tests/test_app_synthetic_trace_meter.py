# SPDX-License-Identifier: Apache-2.0
"""A minted trace id is stored but is not a billable trace at HEAD (CTO-401).

CTO-396 gave every trace-less SDK span its own fresh trace id so a batch of them would stop
collapsing to one row. That was right for storage and wrong for the head meter: before it, a
trace-less span reached ``metering.record_span`` with ``trace_id=None`` and the ``if trace_id:``
guard skipped it, so SDK traffic contributed ZERO ids to the head meter. After it, the same traffic
contributed one per span.

Measured on main before this fix, 300 ``record_llm_call``s with no ``start_trace``:
``rows_stored=300, head meter trace_count=300``. Before CTO-396 the same script gave
``rows_stored=1, trace_count=0``. The fix keeps the id on the wire (so the ClickHouse-derived
invoice count of CTO-390 is unchanged, and the 300 rows stay 300 distinct rows) and marks it
synthetic so only the head meter ignores it: ``rows_stored=300, trace_count=0``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import TRACE_ID_KEY, TRACE_ID_SYNTHETIC_KEY, GenAI
from tally.wire import BatchRequest, encode_request

from gateway.app import app
from gateway.mapping import COLUMNS

_TRACE_COL = COLUMNS.index("TraceId")


class FakeStore:
    def __init__(self) -> None:
        self.spans: list[tuple] = []

    def insert_spans(self, rows: list[tuple]) -> int:
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
def _client() -> Iterator[tuple[TestClient, FakeStore]]:
    with TestClient(app) as client:  # lifespan builds a fresh UsageRollup per startup
        app.state.settings.require_api_key = False
        store = FakeStore()
        app.state.store = store
        yield client, store


def _sdk_client() -> tuple[TallyClient, MemoryExporter]:
    exporter = MemoryExporter()
    client = TallyClient(
        catalog=seed_catalog(),
        exporter=exporter,
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    return client, exporter


def _post(c: TestClient, tenant: str, spans: list[dict]):
    batch = BatchRequest(tenant_id=tenant, sdk_version="test", resource_spans=spans)
    return c.post("/v1/batches", json=json.loads(encode_request(batch)))


def test_three_hundred_trace_less_spans_store_but_add_nothing_to_the_head_meter() -> None:
    """The ticket's measurement, as a test: 300 rows stored, 0 added to the billable trace count."""
    client, exporter = _sdk_client()
    for _ in range(300):
        client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    spans = exporter.spans
    assert len(spans) == 300
    assert all(s[TRACE_ID_SYNTHETIC_KEY] is True for s in spans)

    tenant = "t-cto401-traceless"
    with _client() as (c, store):
        r = _post(c, tenant, spans)
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 300
        # Storage is exactly what CTO-396 made it: 300 rows with 300 distinct trace ids. Keeping
        # the id on the wire is what leaves the ClickHouse-derived invoice count alone.
        assert len(store.spans) == 300
        assert len({row[_TRACE_COL] for row in store.spans}) == 300
        assert app.state.metering.usage(tenant).trace_count == 0


def test_a_real_start_trace_still_counts_exactly_one() -> None:
    client, exporter = _sdk_client()
    with start_trace(feature_tag="checkout_assistant"):
        for _ in range(5):
            client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    spans = exporter.spans
    # A trace the caller started is never marked synthetic, so it meters as it always has.
    assert not any(TRACE_ID_SYNTHETIC_KEY in s for s in spans)

    tenant = "t-cto401-real"
    with _client() as (c, store):
        r = _post(c, tenant, spans)
        assert r.status_code == 200
        assert len(store.spans) == 5
        usage = app.state.metering.usage(tenant)
        assert usage.trace_count == 1
        assert usage.feature_count == 1


def test_an_unmarked_producer_is_metered_exactly_as_before() -> None:
    """Absence of the marker means "real trace", not "unknown": the edge proxy sends no marker."""
    spans = [
        {
            TRACE_ID_KEY: "tr-proxy-1",
            "span_id": f"sp-{i}",
            GenAI.SYSTEM: "openai",
            GenAI.OPERATION_NAME: "chat",
            GenAI.USAGE_INPUT_TOKENS: 10,
        }
        for i in range(4)
    ]
    tenant = "t-cto401-proxy"
    with _client() as (c, store):
        r = _post(c, tenant, spans)
        assert r.status_code == 200
        assert len(store.spans) == 4
        assert app.state.metering.usage(tenant).trace_count == 1


def test_feature_tags_on_trace_less_spans_are_still_metered() -> None:
    """Only the trace id is synthetic. The feature tag is real and still counts (CTO-85)."""
    spans = [
        {
            TRACE_ID_KEY: f"syn-{i}",
            "span_id": f"sp-{i}",
            TRACE_ID_SYNTHETIC_KEY: True,
            GenAI.FEATURE_TAG: "summarizer",
            GenAI.SYSTEM: "openai",
            GenAI.OPERATION_NAME: "chat",
            GenAI.USAGE_INPUT_TOKENS: 10,
        }
        for i in range(10)
    ]
    tenant = "t-cto401-features"
    with _client() as (c, store):
        r = _post(c, tenant, spans)
        assert r.status_code == 200
        assert len(store.spans) == 10
        usage = app.state.metering.usage(tenant)
        assert usage.trace_count == 0
        assert usage.feature_count == 1
