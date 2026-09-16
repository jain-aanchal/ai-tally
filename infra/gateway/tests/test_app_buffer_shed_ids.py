# SPDX-License-Identifier: Apache-2.0
"""A buffer-shed item is named so the client can find it (CTO-405).

Every other shed path names items with ``validation.span_item_id``: ``"{trace}:{span}"`` when the
span carries ids, else ``"#{index}"``. The buffered path named them ``#buffer-overflow-N`` by
INTERNAL overflow position, which identifies nothing the client has ever seen. The items are
RATE_LIMITED and retryable by contract, but a client cannot retry what it cannot identify: the SDK
refuses to guess a mapping and counts them as unmapped loss, so the spend on those spans is lost
permanently on a path that was supposed to be recoverable.

Measured before the fix: buffer at capacity, 4-span batch, 2 shed, client loses 2 spans of billable
spend. After: the 2 shed ids are the client's own, and re-posting exactly those spans recovers them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI

from gateway import app as app_module
from gateway.app import app
from gateway.config import get_settings
from gateway.mapping import COLUMNS
from gateway.validation import span_item_id

_SPAN_COL = COLUMNS.index("SpanId")


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
def _buffered_client(store: FakeStore, *, capacity: int) -> Iterator[TestClient]:
    """A gateway whose ingest buffer is tiny, so an ordinary batch overflows it."""
    settings = get_settings()
    prev = (
        settings.ingest_buffered,
        settings.ingest_buffer_capacity,
        settings.ingest_buffer_poll_interval_s,
    )
    settings.ingest_buffered = True
    settings.ingest_buffer_capacity = capacity
    # Long poll interval: the background drain must NOT quietly empty the buffer mid-test, or the
    # capacity the assertions depend on stops being the capacity.
    settings.ingest_buffer_poll_interval_s = 3600.0
    orig_factory = app_module.ClickHouseStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            app.state.settings.require_api_key = False
            yield client
    finally:
        app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
        (
            settings.ingest_buffered,
            settings.ingest_buffer_capacity,
            settings.ingest_buffer_poll_interval_s,
        ) = prev


def _span(i: int, *, with_ids: bool = True) -> dict:
    span = {
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 10 + i,
    }
    if with_ids:
        span["trace_id"] = "tr-shed"
        span["span_id"] = f"sp-{i}"
    return span


def _post(c: TestClient, spans: list[dict]):
    return c.post(
        "/v1/batches",
        json={"tenant_id": "t-cto405", "sdk_version": "test", "resource_spans": spans},
    )


def _rate_limited_ids(body: dict) -> list[str]:
    return [e["item_id"] for e in body["partial_errors"] if e["code"] == "RATE_LIMITED"]


def test_shed_items_are_named_with_the_client_s_own_span_ids() -> None:
    store = FakeStore()
    spans = [_span(i) for i in range(4)]
    with _buffered_client(store, capacity=2) as c:
        body = _post(c, spans).json()

        shed = _rate_limited_ids(body)
        assert len(shed) == 2
        # Derivable from the spans the client sent, with no knowledge of gateway internals.
        assert shed == [span_item_id(spans[2], 2), span_item_id(spans[3], 3)]
        assert shed == ["tr-shed:sp-2", "tr-shed:sp-3"]
        assert not any(i.startswith("#buffer-overflow") for i in shed)


def test_a_client_following_the_contract_recovers_every_shed_span() -> None:
    """The whole point: the names have to be good enough to drive a real retry."""
    store = FakeStore()
    spans = [_span(i) for i in range(4)]
    with _buffered_client(store, capacity=2) as c:
        shed = _rate_limited_ids(_post(c, spans).json())

        # Drain what was accepted, freeing the buffer, exactly as a real gateway would between the
        # rejection and the client's retry.
        buf = app.state.ingest_buffer
        deadline = time.monotonic() + 30.0
        while buf.depth and time.monotonic() < deadline:
            buf.drain_once()

        # The client maps the shed ids back to its own spans and re-posts precisely those.
        by_id = {span_item_id(s, i): s for i, s in enumerate(spans)}
        retried = [by_id[item_id] for item_id in shed]
        assert len(retried) == 2
        retry_body = _post(c, retried).json()
        assert retry_body["status"] == "accepted"
        assert _rate_limited_ids(retry_body) == []

        deadline = time.monotonic() + 30.0
        while buf.depth and time.monotonic() < deadline:
            buf.drain_once()

        # Nothing was lost: all four spans the client set out to send are stored.
        assert {row[_SPAN_COL] for row in store.spans} == {f"sp-{i}" for i in range(4)}


def test_idless_shed_items_fall_back_to_the_batch_index() -> None:
    """An id-less span has no better name, and "#index" is what every other shed path gives it."""
    store = FakeStore()
    spans = [_span(i, with_ids=False) for i in range(4)]
    with _buffered_client(store, capacity=2) as c:
        shed = _rate_limited_ids(_post(c, spans).json())
        assert shed == ["#2", "#3"]
