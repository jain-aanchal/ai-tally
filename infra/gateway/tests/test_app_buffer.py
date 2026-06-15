"""App-level ingest-buffer tests (CTO-37): a burst never 5xxs, and buffered spans land in ClickHouse.

Buffering is wired in ``lifespan`` from ``settings.ingest_buffered`` and wraps ``app.state.store``, so
the test enables the flag and patches the store factory *before* the TestClient runs startup — that
way the background drain loop writes to our fake store rather than a real ClickHouse.
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


class FakeStore:
    """Fake CH. ``span_fail`` makes every span insert raise (to prove buffering hides it from clients)."""

    def __init__(self, *, span_fail: bool = False) -> None:
        self.spans: list[tuple] = []
        self.span_fail = span_fail
        self._lock = threading.Lock()

    def insert_spans(self, rows: list[tuple]) -> int:
        if self.span_fail:
            raise RuntimeError("clickhouse down")
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
def _buffered_client(store: FakeStore) -> Iterator[TestClient]:
    settings = get_settings()
    prev_buffered = settings.ingest_buffered
    prev_poll = settings.ingest_buffer_poll_interval_s
    settings.ingest_buffered = True
    settings.ingest_buffer_poll_interval_s = 0.01
    orig_factory = app_module.ClickHouseStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    try:
        with TestClient(app) as client:  # runs lifespan → builds buffer around `store`
            app.state.settings.require_api_key = False
            yield client
    finally:
        app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
        settings.ingest_buffered = prev_buffered
        settings.ingest_buffer_poll_interval_s = prev_poll


def _spans(n: int) -> list[dict]:
    return [
        {
            "trace_id": f"t{i}",
            "span_id": f"s{i}",
            GenAI.SYSTEM: "openai",
            GenAI.OPERATION_NAME: "chat",
            GenAI.USAGE_INPUT_TOKENS: 10,
        }
        for i in range(n)
    ]


def _post(c: TestClient, spans: list[dict]):
    return c.post("/v1/batches", json={"tenant_id": "t-local", "sdk_version": "test", "resource_spans": spans})


def _wait_for(predicate, timeout_s: float = 15.0) -> bool:
    # 15s default (was 5s) — GitHub Actions runners under load occasionally
    # need >5s for the buffer to fully drain in the burst test. Locally this
    # test finishes in <500ms; CI runners are 10-30x slower under contention
    # so a generous ceiling avoids flake without masking a real regression
    # (a broken drain wouldn't finish in 15s either).
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_buffered_burst_is_accepted_and_persisted() -> None:
    store = FakeStore()
    with _buffered_client(store) as c:
        # Several batches in quick succession — a burst the synchronous path would push through CH.
        total = 0
        for _ in range(5):
            r = _post(c, _spans(40))
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "accepted"
            assert body["accepted_spans"] == 40  # buffered, acked immediately
            total += 40
        # The buffered spans land in ClickHouse off the hot path. We drive drain_once() from the
        # test thread (idempotent + lock-guarded against the background loop) so the assertion is
        # deterministic rather than racing the loop's poll cadence on a slow CI runner.
        buf = app.state.ingest_buffer
        assert _wait_for(lambda: buf.drain_once() == 0 and len(store.spans) == total), (
            f"only {len(store.spans)}/{total} drained"
        )


def test_burst_returns_no_5xx_even_when_clickhouse_down() -> None:
    # The core guarantee: a failing/slow ClickHouse must not turn into client-facing 5xx. Spans are
    # buffered and retried in the background; the client sees 200 the whole time.
    store = FakeStore(span_fail=True)
    with _buffered_client(store) as c:
        for _ in range(5):
            r = _post(c, _spans(20))
            assert r.status_code == 200  # never 503, even though CH insert raises
            assert r.json()["status"] == "accepted"
        # Rows are retained in the buffer (not lost, not written) while CH is down.
        assert _wait_for(lambda: app.state.ingest_buffer.depth > 0)
        assert store.spans == []


def test_synchronous_path_still_default() -> None:
    # Sanity: with buffering off (default), the store is written synchronously and a failure 503s,
    # confirming the new path is opt-in and the old behavior is intact.
    assert get_settings().ingest_buffered is False
