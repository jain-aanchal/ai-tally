"""App-level tests: capabilities, version negotiation, OTLP endpoint, unknown-field tolerance (CTO-31)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI

from gateway.app import app
from gateway.protocol import INGEST_V1


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


def test_capabilities_endpoint() -> None:
    with _client() as (c, _store):
        r = c.get("/v1/capabilities")
        assert r.status_code == 200
        body = r.json()
        assert INGEST_V1 in body["protocols"]
        assert body["features"]["server_hints"] is True


def test_known_protocol_header_accepted() -> None:
    with _client() as (c, store):
        body = {"tenant_id": "t-local", "sdk_version": "test", "resource_spans": [_good(1)]}
        r = c.post("/v1/batches", json=body, headers={"X-Ingest-Protocol": INGEST_V1})
        assert r.status_code == 200
        assert len(store.spans) == 1


def test_unknown_protocol_header_rejected_400() -> None:
    with _client() as (c, _store):
        body = {"tenant_id": "t-local", "sdk_version": "test", "resource_spans": [_good(1)]}
        r = c.post("/v1/batches", json=body, headers={"X-Ingest-Protocol": "ingest-v999"})
        assert r.status_code == 400


def test_unknown_top_level_fields_tolerated() -> None:
    """Additive contract: a future client may add envelope fields; an older gateway ignores them."""
    with _client() as (c, store):
        body = {
            "tenant_id": "t-local",
            "sdk_version": "test",
            "resource_spans": [_good(1)],
            "future_field": {"some": "thing"},  # unknown — must not break ingest
            "another_new_knob": 42,
        }
        r = c.post("/v1/batches", json=body)
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
        assert len(store.spans) == 1


def test_otlp_traces_endpoint_ingests() -> None:
    with _client() as (c, store):
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
                                    "traceId": "abc",
                                    "spanId": "def",
                                    "startTimeUnixNano": "1717000000000000000",
                                    "attributes": [
                                        {"key": GenAI.SYSTEM, "value": {"stringValue": "openai"}},
                                        {
                                            "key": GenAI.OPERATION_NAME,
                                            "value": {"stringValue": "chat"},
                                        },
                                        {
                                            "key": GenAI.USAGE_INPUT_TOKENS,
                                            "value": {"intValue": "55"},
                                        },
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        r = c.post("/v1/otlp/traces", json=otlp, headers={"X-Tenant-Id": "t-local"})
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
        assert len(store.spans) == 1
