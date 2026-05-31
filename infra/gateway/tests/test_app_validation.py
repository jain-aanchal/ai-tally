"""App-level tests for validation wiring → PARTIAL / REJECTED responses (CTO-34).

Auth is disabled (require_api_key=False) and the ClickHouse store is replaced with an in-memory
fake, so accepted spans take the write path without any running infra.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI

from gateway.app import app


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


def _good(i: int) -> dict:
    return {
        "trace_id": f"t{i}",
        "span_id": f"s{i}",
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 10,
    }


def _post(c: TestClient, spans: list[dict]) -> dict:
    body = {"tenant_id": "t-local", "sdk_version": "test", "resource_spans": spans}
    return c.post("/v1/batches", json=body)


def test_all_good_spans_accepted() -> None:
    with _client() as (c, store):
        r = _post(c, [_good(1), _good(2)])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["accepted_spans"] == 2
        assert body["partial_errors"] == []
        assert len(store.spans) == 2


def test_one_bad_span_yields_partial() -> None:
    bad = _good(2)
    bad["email"] = "alice@example.com"  # PII → rejected
    with _client() as (c, store):
        r = _post(c, [_good(1), bad])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "partial"
        assert body["accepted_spans"] == 1  # only the good one written
        codes = {e["code"] for e in body["partial_errors"]}
        assert "PII_DETECTED" in codes
        assert len(store.spans) == 1


def test_all_bad_spans_yields_rejected() -> None:
    bad1 = _good(1)
    bad1[GenAI.USAGE_INPUT_TOKENS] = -5  # invalid schema
    bad2 = _good(2)
    bad2["user_email"] = "bob@example.com"  # forbidden PII key
    with _client() as (c, store):
        r = _post(c, [bad1, bad2])
        assert r.status_code == 422
        body = r.json()
        assert body["status"] == "rejected"
        assert body["accepted_spans"] == 0
        assert len(body["partial_errors"]) == 2
        assert store.spans == []
