"""Ingest-path replay capture wiring (CTO-237).

Proves that /v1/batches now populates the opt-in replay corpus when (and only when) the tenant has
replay enabled, and that the captured samples are what /v1/replay projects from. Auth is disabled
and the ClickHouse store is an in-memory fake, so the ingest write path runs without any infra.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from tally.schema import GenAI

from gateway.app import app
from gateway.replay_store import InMemoryReplayBlobStore
from gateway.tenant_replay import ReplayConfig

T = "t-local"


class FakeStore:
    """In-memory ClickHouse stand-in that also records replay-sample writeback."""

    def __init__(self) -> None:
        self.spans: list[tuple] = []
        self.replay_samples: list = []

    def insert_spans(self, rows: list[tuple]) -> int:
        self.spans.extend(rows)
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def insert_replay_samples(self, rows: list) -> int:
        self.replay_samples.extend(rows)
        return len(rows)

    def recent_replay_samples(self, limit: int) -> list:
        return []

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeReplayStore:
    """In-memory TenantReplayStore stand-in, no Postgres."""

    def __init__(self, cfg: ReplayConfig) -> None:
        self._cfg = cfg

    def get(self, tenant_id: str) -> ReplayConfig:
        return self._cfg


@contextmanager
def _client(cfg: ReplayConfig) -> Iterator[tuple[TestClient, FakeStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        store = FakeStore()
        app.state.store = store
        app.state.tenant_replay = FakeReplayStore(cfg)
        app.state.replay_blob_store = InMemoryReplayBlobStore()
        app.state.replay_sample_index = []
        app.state.replay_runs = []
        if hasattr(app.state, "replay_candidate_client"):
            delattr(app.state, "replay_candidate_client")
        yield client, store


def _span(i: int) -> dict:
    return {
        "trace_id": f"t{i}",
        "span_id": f"s{i}",
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.FEATURE_TAG: "research",
        GenAI.REQUEST_MODEL: "gpt-4o",
        GenAI.RESPONSE_MODEL: "gpt-4o",
        GenAI.USAGE_INPUT_TOKENS: 100 + i,
        GenAI.USAGE_OUTPUT_TOKENS: 40 + i,
    }


def _post(c: TestClient, spans: list[dict]) -> dict:
    body = {"tenant_id": T, "sdk_version": "test", "resource_spans": spans}
    r = c.post("/v1/batches", json=body)
    assert r.status_code == 200, r.text
    return r.json()


_ENABLED = ReplayConfig(
    enabled=True, sample_rate=1.0, retention_days=30, daily_budget_usd=Decimal("5.00")
)
_DISABLED = ReplayConfig(
    enabled=False, sample_rate=0.05, retention_days=30, daily_budget_usd=Decimal("5.00")
)


def test_enabled_tenant_captures_into_index_and_clickhouse() -> None:
    with _client(_ENABLED) as (c, store):
        body = _post(c, [_span(i) for i in range(8)])
        assert body["status"] == "accepted"
        # sample_rate=1.0 => every accepted span is captured.
        index = app.state.replay_sample_index
        assert len(index) == 8
        assert {r.tenant_id for r in index} == {T}
        assert {r.feature_tag for r in index} == {"research"}
        # Token counts survive onto the index row (the mock candidate client replays off them).
        assert all(r.input_tokens > 0 and r.output_tokens > 0 for r in index)
        assert all(r.pii_scrubbed for r in index)
        # Durably mirrored into ClickHouse for restart re-hydration.
        assert len(store.replay_samples) == 8


def test_enabled_tenant_replay_projection_sees_the_corpus() -> None:
    with _client(_ENABLED) as (c, _store):
        _post(c, [_span(i) for i in range(6)])
        r = c.post(
            "/v1/replay",
            headers={"X-Tenant-Id": T},
            json={
                "tenant_id": T,
                "feature_tag": "research",
                "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4-5"}],
                "sample_size": 50,
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["samples_available"] == 6
        assert len(payload["per_candidate"]) == 1
        assert payload["per_candidate"][0]["samples_replayed"] > 0


def test_disabled_tenant_captures_nothing() -> None:
    with _client(_DISABLED) as (c, store):
        body = _post(c, [_span(i) for i in range(8)])
        assert body["status"] == "accepted"
        assert len(store.spans) == 8  # ingest still writes spans
        # ...but nothing is captured for a tenant that has not opted in.
        assert app.state.replay_sample_index == []
        assert store.replay_samples == []


def test_capture_never_breaks_ingest_when_writeback_raises() -> None:
    """A ClickHouse replay-sample writeback failure must not fail the accepted ingest (CTO-237)."""
    class BoomStore(FakeStore):
        def insert_replay_samples(self, rows: list) -> int:
            raise RuntimeError("clickhouse down")

    with TestClient(app) as c:
        app.state.settings.require_api_key = False
        store = BoomStore()
        app.state.store = store
        app.state.tenant_replay = FakeReplayStore(_ENABLED)
        app.state.replay_blob_store = InMemoryReplayBlobStore()
        app.state.replay_sample_index = []
        app.state.replay_runs = []
        body = _post(c, [_span(1), _span(2)])
        assert body["status"] == "accepted"
        assert body["accepted_spans"] == 2
        # In-memory capture still succeeded even though the durable writeback raised.
        assert len(app.state.replay_sample_index) == 2


@pytest.mark.parametrize("cap", [3])
def test_in_memory_index_is_bounded_newest_wins(cap: int) -> None:
    from gateway import app as app_module

    with _client(_ENABLED) as (c, _store):
        original = app_module.REPLAY_INDEX_PER_TENANT_CAP
        app_module.REPLAY_INDEX_PER_TENANT_CAP = cap
        try:
            _post(c, [_span(i) for i in range(2)])
            _post(c, [_span(i) for i in range(2, 7)])  # 7 captured total, cap is 3
        finally:
            app_module.REPLAY_INDEX_PER_TENANT_CAP = original
        index = app.state.replay_sample_index
        assert len(index) == cap
        # Newest-wins: the last three trace ids captured are retained.
        assert [r.trace_id for r in index] == ["t4", "t5", "t6"]
