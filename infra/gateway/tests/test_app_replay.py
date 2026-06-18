"""End-to-end tests for /v1/replay + /v1/tenant/replay/config (CTO-113)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.replay_store import (
    InMemoryReplayBlobStore,
    ReplaySampleRow,
    build_replay_object_key,
)
from gateway.tenant_replay import ReplayConfig

T = "t-acme"


class FakeReplayStore:
    """In-memory stand-in for TenantReplayStore — no Postgres."""

    def __init__(self) -> None:
        self._cfg: dict[str, ReplayConfig] = {}

    def get(self, tenant_id: str) -> ReplayConfig:
        return self._cfg.get(
            tenant_id,
            ReplayConfig(
                enabled=False, sample_rate=0.05, retention_days=30,
                daily_budget_usd=Decimal("5.00"),
            ),
        )

    def upsert(self, tenant_id: str, **kwargs) -> ReplayConfig:
        current = self.get(tenant_id)
        new = ReplayConfig(
            enabled=current.enabled if kwargs.get("enabled") is None else bool(kwargs["enabled"]),
            sample_rate=current.sample_rate if kwargs.get("sample_rate") is None
                else float(kwargs["sample_rate"]),
            retention_days=current.retention_days if kwargs.get("retention_days") is None
                else int(kwargs["retention_days"]),
            daily_budget_usd=current.daily_budget_usd if kwargs.get("daily_budget_usd") is None
                else Decimal(str(kwargs["daily_budget_usd"])),
        )
        if not (0.0 <= new.sample_rate <= 1.0):
            raise ValueError("sample_rate must be between 0 and 1")
        if new.retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if new.daily_budget_usd < 0:
            raise ValueError("daily_budget_usd must be non-negative")
        self._cfg[tenant_id] = new
        return new


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_replay = FakeReplayStore()
        app.state.replay_blob_store = InMemoryReplayBlobStore()
        app.state.replay_sample_index = []
        app.state.replay_runs = []
        # Strip any prod candidate-client override.
        if hasattr(app.state, "replay_candidate_client"):
            delattr(app.state, "replay_candidate_client")
        yield c


# --- Config endpoints -------------------------------------------------------------

def test_config_defaults_off(client: TestClient) -> None:
    r = client.get("/v1/tenant/replay/config", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is False
    assert r.json()["config"]["sample_rate"] == 0.05


def test_config_round_trip(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/replay/config",
        headers={"X-Tenant-Id": T},
        json={"enabled": True, "sample_rate": 0.1, "daily_budget_usd": 10.0},
    )
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is True
    assert r.json()["config"]["sample_rate"] == 0.1
    assert r.json()["config"]["daily_budget_usd"] == 10.0


def test_config_rejects_bad_sample_rate(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/replay/config",
        headers={"X-Tenant-Id": T},
        json={"sample_rate": 2.0},
    )
    assert r.status_code == 422


# --- Projection endpoint ----------------------------------------------------------

def _seed_samples(client: TestClient, n: int = 50, *, feature_tag: str = "chat") -> None:
    """Pre-load n scrubbed samples into the blob + index so /v1/replay has corpus to replay."""
    blob_store = app.state.replay_blob_store
    index = app.state.replay_sample_index
    now = datetime.now(timezone.utc)
    for i in range(n):
        sid = uuid4()
        key = build_replay_object_key(T, sid, now)
        input_tokens = 100 + i * 5
        output_tokens = 50 + i * 2
        body = json.dumps({
            "prompt": f"sample {i}",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }).encode()
        blob_store.put_bytes(key, body)
        index.append(ReplaySampleRow(
            tenant_id=T,
            sample_id=sid,
            trace_id=f"trace-{i}",
            feature_tag=feature_tag,
            real_provider="anthropic",
            real_model="claude-sonnet-4.5",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            captured_at=now,
            s3_object_key=key,
            pii_scrubbed=True,
        ))


def test_replay_projection_empty_tenant(client: TestClient) -> None:
    r = client.post(
        "/v1/replay",
        headers={"X-Tenant-Id": T},
        json={
            "candidate_models": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["samples_available"] == 0
    assert body["per_candidate"] == []


def test_replay_projection_returns_per_candidate_rows(client: TestClient) -> None:
    _seed_samples(client, n=50)
    r = client.post(
        "/v1/replay",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_models": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
                {"provider": "openai", "model": "gpt-5-mini"},
            ],
            "sample_size": 20,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["samples_available"] == 50
    assert len(body["per_candidate"]) == 2
    for row in body["per_candidate"]:
        assert row["samples_replayed"] > 0
        # Projected monthly cost should be a positive number scaled off real catalog pricing.
        assert row["projected_monthly_cost_micro_usd"] > 0
        assert 0.0 <= row["error_rate"] <= 1.0
    # Diagnostics carry the v1 honesty string.
    diag = body["diagnostics"]
    assert diag["context_fidelity"] == "resolved-context replay (no live retrieval)"
    assert diag["replay_cost_micro_usd"] >= 0


def test_replay_projection_filters_by_feature_tag(client: TestClient) -> None:
    _seed_samples(client, n=30, feature_tag="chat")
    _seed_samples(client, n=20, feature_tag="research")
    r = client.post(
        "/v1/replay",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "feature_tag": "research",
            "candidate_models": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["samples_available"] == 20
