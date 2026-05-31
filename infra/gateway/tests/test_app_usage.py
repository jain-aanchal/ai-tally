"""End-to-end metering through the gateway: HEAD count via ingest + GET /v1/usage (CTO-84/85/86)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.metering import UsageRollup
from tally.schema import GenAI
from tally.wire import IdempotencyCache

T = "t-acme"


class _FakeStore:
    """Stand-in for ClickHouseStore — accepts writes, no real infra."""

    def ping(self) -> bool:
        return True

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int:
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list[object]) -> int:
        return len(events)

    def insert_identity_links(self, tenant_id: str, links: list[object]) -> int:
        return len(links)

    def close(self) -> None:
        pass


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        # Lifespan has populated real store/auth; swap the store for a fake so no infra is needed,
        # and reset per-test metering/idempotency so counts don't bleed between tests.
        app.state.store = _FakeStore()
        app.state.metering = UsageRollup()
        app.state.idempotency = IdempotencyCache(ttl_seconds=3600)
        yield c


def _span(trace_id: str, feature_tag: str) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        GenAI.FEATURE_TAG: feature_tag,
        GenAI.SYSTEM: "openai",
        GenAI.REQUEST_MODEL: "gpt-4o-mini",
        GenAI.USAGE_INPUT_TOKENS: 100,
        GenAI.USAGE_OUTPUT_TOKENS: 20,
    }


def _batch(spans: list[dict[str, object]], *, sample_rate: float = 1.0) -> dict[str, object]:
    return {
        "tenant_id": T,
        "resource_spans": spans,
        "sampling": {"head_sample_rate": sample_rate},
    }


def test_ingest_meters_distinct_traces_and_features(client: TestClient) -> None:
    spans = [
        _span("trace_1", "checkout"),
        _span("trace_2", "checkout"),
        _span("trace_3", "search"),
    ]
    assert client.post("/v1/batches", json=_batch(spans)).status_code == 200

    usage = client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()
    assert usage["trace_count"] == 3
    assert usage["feature_count"] == 2  # {checkout, search}
    assert usage["tenant_id"] == T


def test_billed_count_is_independent_of_sample_rate(client: TestClient) -> None:
    # A heavily-sampled batch (1%) must still bill every trace — metering is at HEAD.
    spans = [_span(f"trace_{i}", "checkout") for i in range(10)]
    assert client.post("/v1/batches", json=_batch(spans, sample_rate=0.01)).status_code == 200

    usage = client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()
    assert usage["trace_count"] == 10


def test_replayed_batch_does_not_double_count(client: TestClient) -> None:
    spans = [_span("trace_1", "checkout"), _span("trace_2", "checkout")]
    body = _batch(spans)
    body["batch_id"] = "batch-fixed-1"
    first = client.post("/v1/batches", json=body)
    second = client.post("/v1/batches", json=body)  # idempotent replay
    assert first.status_code == 200
    assert second.json()["replayed"] is True

    usage = client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()
    assert usage["trace_count"] == 2  # replay returns cached response, re-runs no metering


def test_usage_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/usage").status_code == 422
