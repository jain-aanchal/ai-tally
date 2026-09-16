# SPDX-License-Identifier: Apache-2.0
"""A batch keeps every span it was sent, id-less or not (CTO-396).

The bug: ``BatchRequest.deduplicated()`` keyed each span on ``(trace_id, span_id)``, and the Python
SDK put neither on the wire, so every span in a batch keyed to ``(None, None)`` and all but the
first were discarded before validation and before the write. The gateway answered 200 /
``accepted``, so the loss was invisible on both ends.

The end-to-end test here is the one whose absence hid it: spans built through the real SDK path,
posted to the real app with a fake store, asserting that the stored row count equals the number of
spans sent.
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
from tally.schema import SPAN_ID_KEY, TRACE_ID_KEY, GenAI
from tally.wire import BatchRequest, encode_request

from gateway.app import app
from gateway.mapping import COLUMNS

_TRACE_COL = COLUMNS.index("TraceId")
_SPAN_COL = COLUMNS.index("SpanId")


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
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        store = FakeStore()
        app.state.store = store
        yield client, store


def _post(c: TestClient, spans: list[dict]):
    body = {"tenant_id": "t-cto396", "sdk_version": "test", "resource_spans": spans}
    return c.post("/v1/batches", json=body)


def _span_without_ids(tokens: int = 10) -> dict:
    return {
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: tokens,
    }


def _sdk_spans(n: int) -> list[dict]:
    """Spans built through the real SDK emit path, exactly as a customer's process produces them."""
    exporter = MemoryExporter()
    client = TallyClient(
        catalog=seed_catalog(),
        exporter=exporter,
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    with start_trace(feature_tag="checkout_assistant"):
        for _ in range(n):
            client.record_llm_call(
                provider="openai", model="gpt-5-mini", usage=Usage(10, 5)
            )
    return exporter.spans


def test_multi_span_batch_with_no_ids_stores_every_span() -> None:
    with _client() as (c, store):
        r = _post(c, [_span_without_ids(i) for i in range(1, 6)])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["accepted_spans"] == 5
        assert len(store.spans) == 5


def test_genuine_duplicate_still_collapses_to_one() -> None:
    dup = {**_span_without_ids(), "trace_id": "tr-1", "span_id": "sp-1"}
    with _client() as (c, store):
        r = _post(c, [dict(dup), dict(dup)])
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 1
        assert len(store.spans) == 1


def test_duplicate_collapses_while_id_less_spans_survive() -> None:
    dup = {**_span_without_ids(), "trace_id": "tr-1", "span_id": "sp-1"}
    with _client() as (c, store):
        r = _post(c, [dict(dup), dict(dup), _span_without_ids(1), _span_without_ids(2)])
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 3
        assert len(store.spans) == 3


def test_end_to_end_real_sdk_spans_are_all_stored() -> None:
    spans = _sdk_spans(5)
    assert len(spans) == 5
    with _client() as (c, store):
        batch = BatchRequest(
            tenant_id="t-cto396", sdk_version="test", resource_spans=spans
        )
        r = c.post("/v1/batches", json=json.loads(encode_request(batch)))
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == len(spans)
        assert len(store.spans) == len(spans)

    # The SDK's own ids reach the TraceId / SpanId columns rather than the mapper's per-row
    # fallback: one trace, five distinct spans.
    assert len({row[_SPAN_COL] for row in store.spans}) == 5
    assert {row[_TRACE_COL] for row in store.spans} == {spans[0][TRACE_ID_KEY]}
    assert {row[_SPAN_COL] for row in store.spans} == {s[SPAN_ID_KEY] for s in spans}


def test_five_hundred_sdk_spans_store_five_hundred_rows() -> None:
    spans = _sdk_spans(500)
    with _client() as (c, store):
        batch = BatchRequest(
            tenant_id="t-cto396", sdk_version="test", resource_spans=spans
        )
        r = c.post("/v1/batches", json=json.loads(encode_request(batch)))
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 500
        assert len(store.spans) == 500
        assert len({row[_SPAN_COL] for row in store.spans}) == 500


def test_otlp_traces_keep_every_span() -> None:
    """The OTLP path runs the same pipeline, so it goes through deduplicated() too.

    OTLP spans carry real ids, so it was never hit by the collapse; this pins that down rather than
    leaving it asserted only in prose.
    """
    otlp = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "svc"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "tr-otlp",
                                "spanId": f"sp-{i}",
                                "startTimeUnixNano": "1700000000000000000",
                                "attributes": [
                                    {
                                        "key": GenAI.SYSTEM,
                                        "value": {"stringValue": "openai"},
                                    },
                                    {
                                        "key": GenAI.OPERATION_NAME,
                                        "value": {"stringValue": "chat"},
                                    },
                                ],
                            }
                            for i in range(4)
                        ]
                    }
                ],
            }
        ]
    }
    with _client() as (c, store):
        r = c.post("/v1/otlp/traces", json=otlp, headers={"X-Tenant-Id": "t-cto396"})
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 4
        assert len(store.spans) == 4
