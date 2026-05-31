"""App-level tests: server_hints round-trip, overload shedding, retry semantics (CTO-36).

Auth is off and the store is a fake, so accepted spans take the write path without infra.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI
from tally.wire import ServerHints

from gateway.app import app
from gateway.backpressure import Backpressure, is_retryable


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.spans: list[tuple] = []
        self.fail = fail

    def insert_spans(self, rows: list[tuple]) -> int:
        if self.fail:
            raise RuntimeError("clickhouse down")
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
def _client(*, store_fail: bool = False) -> Iterator[tuple[TestClient, FakeStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        store = FakeStore(fail=store_fail)
        app.state.store = store
        yield client, store


def _good(i: int) -> dict:
    return {
        "trace_id": f"t{i}",
        "span_id": f"s{i}",
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 10,
    }


def _post(c: TestClient, spans: list[dict]):
    body = {"tenant_id": "t-local", "sdk_version": "test", "resource_spans": spans}
    return c.post("/v1/batches", json=body)


def test_healthy_response_carries_baseline_hints() -> None:
    with _client() as (c, _store):
        r = _post(c, [_good(1)])
        assert r.status_code == 200
        hints = r.json()["server_hints"]
        assert hints["sample_rate_override"] is None
        assert hints["retry_after_ms"] == 0
        assert hints["max_batch_size"] >= 1


def test_overload_sheds_overflow_as_retryable_partial() -> None:
    # soft_limit=1 → the in-flight request itself trips overload; cap batch to 2 items.
    with _client() as (c, store):
        app.state.backpressure = Backpressure(soft_limit=1, overload_max_batch=2)
        r = _post(c, [_good(1), _good(2), _good(3), _good(4)])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "partial"
        assert body["accepted_spans"] == 2  # only the admitted prefix written
        assert len(store.spans) == 2
        shed = [e for e in body["partial_errors"] if e["code"] == "RATE_LIMITED"]
        assert len(shed) == 2  # the two overflow spans
        # Tightened hints tell a conformant client to back off.
        hints = body["server_hints"]
        assert hints["sample_rate_override"] == 0.25
        assert hints["retry_after_ms"] > 0
    app.state.backpressure = Backpressure()  # restore default for other tests


def test_store_failure_is_retryable_503() -> None:
    with _client(store_fail=True) as (c, _store):
        r = _post(c, [_good(1)])
        assert r.status_code == 503
        assert r.json()["status"] == "retry"
        assert is_retryable(r.status_code) is True


def test_conformant_client_honors_hints_round_trip() -> None:
    """A minimal client that resends shed items in hint-sized batches eventually lands them all."""

    class ConformantClient:
        def __init__(self) -> None:
            self.max_batch_size = 1000

        def apply(self, hints: dict) -> None:
            self.max_batch_size = hints["max_batch_size"]

    with _client() as (c, store):
        app.state.backpressure = Backpressure(soft_limit=1, overload_max_batch=2)
        client = ConformantClient()
        pending = [_good(i) for i in range(5)]
        rounds = 0
        while pending and rounds < 10:
            rounds += 1
            send = pending[: client.max_batch_size]
            remainder = pending[client.max_batch_size :]
            r = _post(c, send)
            body = r.json()
            client.apply(body["server_hints"])
            shed_ids = {e["item_id"] for e in body["partial_errors"] if e["code"] == "RATE_LIMITED"}
            # Resend what the server shed, plus anything that didn't fit this round's ceiling.
            reshed = [s for s in send if f"{s['trace_id']}:{s['span_id']}" in shed_ids]
            pending = remainder + reshed
        assert pending == []  # everything eventually accepted
        assert len(store.spans) == 5
    app.state.backpressure = Backpressure()


def test_server_hints_dataclass_defaults() -> None:
    h = ServerHints()
    assert h.flush_interval_ms == 5000
    assert h.max_batch_size == 1000
    assert h.sample_rate_override is None
    assert h.retry_after_ms == 0
