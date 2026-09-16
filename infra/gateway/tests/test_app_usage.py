"""End-to-end metering through the gateway: HEAD count via ingest + GET /v1/usage (CTO-84/85/86).

CTO-390 split what this file covers. ``/v1/usage`` no longer reads the in-process meter: it answers
from the durable, shared source (ClickHouse for an open period, Postgres for a committed one), so the
endpoint tests drive that source. The HEAD meter is still asserted directly, because the CTO-84
property (the billed count does not fall when analytics sampling does) is about the meter, not about
the endpoint that used to expose it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.metering import UsageRollup
from gateway.usage_store import DurableUsageRollup, UsageUnavailable
from tally.schema import GenAI
from tally.wire import IdempotencyCache

T = "t-acme"


class _FakeStore:
    """Stand-in for ClickHouseStore: accepts writes, no real infra."""

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


class _FakeCounts:
    """The durable, shared count /v1/usage reads (ClickHouse in production)."""

    def __init__(self) -> None:
        self.traces = 0
        self.features = 0
        self.unavailable = False

    def usage_counts(
        self, tenant_id: str, *, period_start: datetime, period_end: datetime
    ) -> tuple[int, int]:
        if self.unavailable:
            raise RuntimeError("clickhouse unreachable")
        return self.traces, self.features


class _FakeCommitted:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}

    def get(self, tenant_id: str, period: str):  # noqa: ANN201 - mirrors the real store's return
        return self.rows.get((tenant_id, period))


@dataclass
class _Harness:
    client: TestClient
    counts: _FakeCounts
    committed: _FakeCommitted


def _wire_usage(counts: _FakeCounts, committed: _FakeCommitted) -> None:
    """Point the endpoint at the fake durable sources, with caching off so tests see each read."""
    app.state.usage = DurableUsageRollup(
        counts_source=lambda: counts,
        committed=committed,
        plan_limits=app.state.metering.plan_limit_for,
        cache_ttl_s=0.0,
    )


@pytest.fixture
def h() -> Iterator[_Harness]:
    with TestClient(app) as c:
        # Lifespan has populated real store/auth; swap the store for a fake so no infra is needed,
        # and reset per-test metering/idempotency so counts don't bleed between tests.
        app.state.store = _FakeStore()
        app.state.metering = UsageRollup()
        app.state.idempotency = IdempotencyCache(ttl_seconds=3600)
        counts, committed = _FakeCounts(), _FakeCommitted()
        _wire_usage(counts, committed)
        yield _Harness(client=c, counts=counts, committed=committed)


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


# --- the HEAD meter (CTO-84/85), which ingest still maintains --------------------------------------


def test_ingest_meters_distinct_traces_and_features(h: _Harness) -> None:
    spans = [
        _span("trace_1", "checkout"),
        _span("trace_2", "checkout"),
        _span("trace_3", "search"),
    ]
    assert h.client.post("/v1/batches", json=_batch(spans)).status_code == 200

    metered = app.state.metering.usage(T)
    assert metered.trace_count == 3
    assert metered.feature_count == 2  # {checkout, search}


def test_billed_count_is_independent_of_sample_rate(h: _Harness) -> None:
    # A heavily-sampled batch (1%) must still bill every trace: metering is at HEAD.
    spans = [_span(f"trace_{i}", "checkout") for i in range(10)]
    assert h.client.post("/v1/batches", json=_batch(spans, sample_rate=0.01)).status_code == 200

    assert app.state.metering.usage(T).trace_count == 10


def test_replayed_batch_does_not_double_count(h: _Harness) -> None:
    spans = [_span("trace_1", "checkout"), _span("trace_2", "checkout")]
    body = _batch(spans)
    body["batch_id"] = "batch-fixed-1"
    first = h.client.post("/v1/batches", json=body)
    second = h.client.post("/v1/batches", json=body)  # idempotent replay
    assert first.status_code == 200
    assert second.json()["replayed"] is True

    # Replay returns the cached response and re-runs no metering.
    assert app.state.metering.usage(T).trace_count == 2


# --- GET /v1/usage now reads the durable, shared source (CTO-390) ----------------------------------


def test_usage_answers_from_the_durable_source(h: _Harness) -> None:
    h.counts.traces, h.counts.features = 4321, 12
    usage = h.client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()
    assert usage["trace_count"] == 4321
    assert usage["feature_count"] == 12
    assert usage["tenant_id"] == T


def test_usage_does_not_read_the_in_process_meter(h: _Harness) -> None:
    """THE BUG. What one replica happens to hold in memory is not this tenant's usage."""
    assert h.client.post("/v1/batches", json=_batch([_span("trace_1", "checkout")])).status_code == 200
    h.counts.traces, h.counts.features = 900, 5  # what the shared store actually holds

    usage = h.client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()

    assert usage["trace_count"] == 900  # not the 1 this process metered
    assert app.state.metering.usage(T).trace_count == 1  # the HEAD meter is untouched


def test_usage_survives_a_restart(h: _Harness) -> None:
    """Re-enter the lifespan (a redeployed worker) and read the same figures from the same store."""
    h.counts.traces, h.counts.features = 77, 3
    before = h.client.get("/v1/usage", headers={"X-Tenant-Id": T}).json()

    with TestClient(app) as restarted:
        app.state.store = _FakeStore()
        app.state.metering = UsageRollup()
        _wire_usage(h.counts, h.committed)  # same durable store, brand new process state
        after = restarted.get("/v1/usage", headers={"X-Tenant-Id": T}).json()

    assert before["trace_count"] == after["trace_count"] == 77
    assert after["feature_count"] == 3


def test_an_unavailable_source_is_an_explicit_unknown_not_a_zero(h: _Harness) -> None:
    """503 with nulls. A 0 would look exactly like a tenant who has not used the product."""
    h.counts.traces, h.counts.features = 500, 8
    h.counts.unavailable = True

    resp = h.client.get("/v1/usage", headers={"X-Tenant-Id": T})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "USAGE_UNAVAILABLE"
    assert body["trace_count"] is None
    assert body["feature_count"] is None


def test_a_committed_period_is_served_frozen(h: _Harness) -> None:
    from gateway.metering import UsageRecord

    h.counts.traces = 2  # ClickHouse has aged most of the month out
    h.committed.rows[(T, "2026-05")] = UsageRecord(
        tenant_id=T,
        period="2026-05",
        trace_count=1234,
        feature_count=6,
        trace_commitment="frozen",
        feature_commitment="frozen",
        plan="free",
        trace_limit=None,
        feature_limit=None,
        closed=True,
    )

    usage = h.client.get("/v1/usage?period=2026-05", headers={"X-Tenant-Id": T}).json()

    assert usage["trace_count"] == 1234
    assert usage["closed"] is True


def test_a_malformed_period_is_rejected(h: _Harness) -> None:
    assert h.client.get("/v1/usage?period=2026-13", headers={"X-Tenant-Id": T}).status_code == 422


def test_usage_requires_tenant_when_auth_disabled(h: _Harness) -> None:
    assert h.client.get("/v1/usage").status_code == 422


def test_usage_unavailable_is_not_silently_swallowed() -> None:
    """The exception type is part of the contract: callers must not be able to read it as empty."""
    assert issubclass(UsageUnavailable, RuntimeError)
