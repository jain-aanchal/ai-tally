"""App-level tests for the CDP /v1/events convenience endpoint (CTO-105).

The endpoint is a thin wrapper that turns a single event POST (or an `events`
list) into a zero-span BatchRequest, so the same auth/rate-limit/idempotency
plumbing applies. We exercise the wrapper, not the underlying pipeline (which
has dedicated tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from gateway.app import app


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, list]] = []

    def insert_spans(self, rows: list[tuple]) -> int:
        return 0

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        self.events.append((tenant_id, list(events)))
        return len(events)

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


def _event(name: str = "positive_feedback") -> dict:
    return {
        "event_name": name,
        "user_id_hash": "a" * 64,
        "occurred_at_ns": 1_700_000_000_000_000_000,
        "value_currency": "USD",
        "value_type": "count",
        "source": "vercel-chatbot-demo",
    }


def test_single_event_accepted() -> None:
    with _client() as (c, store):
        r = c.post(
            "/v1/events",
            json={"tenant_id": "t-local", **_event()},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        # Wrapper builds a zero-span batch, so accepted_spans is 0 but the
        # event went through.
        assert len(store.events) == 1
        tenant, evs = store.events[0]
        assert tenant == "t-local"
        assert evs[0].event_name == "positive_feedback"
        assert evs[0].source == "vercel-chatbot-demo"


def test_events_list_accepted() -> None:
    with _client() as (c, store):
        r = c.post(
            "/v1/events",
            json={
                "tenant_id": "t-local",
                "events": [_event("positive_feedback"), _event("conversion")],
            },
        )
        assert r.status_code == 200
        assert len(store.events) == 1
        names = {ev.event_name for ev in store.events[0][1]}
        assert names == {"positive_feedback", "conversion"}


def test_tenant_id_required() -> None:
    with _client() as (c, _store):
        r = c.post("/v1/events", json={"events": [_event()]})
        assert r.status_code == 422


def test_malformed_event_rejected() -> None:
    with _client() as (c, _store):
        # Missing required 'user_id_hash' field
        r = c.post(
            "/v1/events",
            json={"tenant_id": "t-local", "events": [{"event_name": "x"}]},
        )
        assert r.status_code == 422


def test_empty_events_list_rejected() -> None:
    with _client() as (c, _store):
        r = c.post("/v1/events", json={"tenant_id": "t-local", "events": []})
        assert r.status_code == 422
