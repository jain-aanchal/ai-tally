"""Tests for POST /v1/replay/estimate — body-driven what-if (CTO-128)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.replay_estimate import apply_system_prompt_override
from gateway.replay_executor import CandidateCall, CandidateResponse
from gateway.replay_store import (
    InMemoryReplayBlobStore,
    ReplaySampleRow,
    build_replay_object_key,
)
from gateway.tenant_replay import ReplayConfig

T = "t-acme"


class FakeReplayStore:
    def __init__(self) -> None:
        self._cfg: dict[str, ReplayConfig] = {}

    def get(self, tenant_id: str) -> ReplayConfig:
        return self._cfg.get(
            tenant_id,
            ReplayConfig(
                enabled=False, sample_rate=0.05, retention_days=30,
                daily_budget_usd=Decimal("50.00"),
            ),
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_replay = FakeReplayStore()
        app.state.replay_blob_store = InMemoryReplayBlobStore()
        app.state.replay_sample_index = []
        app.state.replay_runs = []
        if hasattr(app.state, "replay_candidate_client"):
            delattr(app.state, "replay_candidate_client")
        yield c


def _seed_samples(n: int = 50, *, feature_tag: str = "chat", system_prompt: str = "short") -> None:
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
            "system_prompt": system_prompt,
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


# --- helper unit tests ---------------------------------------------------------------

def test_override_adjusts_input_tokens_up() -> None:
    env = {"system_prompt": "tiny", "input_tokens": 100, "output_tokens": 50}
    # 'tiny' ~ 1 token; a 400-char override ~ 100 tokens => +99 delta.
    out = apply_system_prompt_override(env, "x" * 400)
    assert out["input_tokens"] == 100 - (len("tiny") // 4) + (400 // 4)
    assert out["system_prompt"] == "x" * 400
    # original envelope untouched.
    assert env["input_tokens"] == 100


def test_override_none_is_passthrough() -> None:
    env = {"system_prompt": "tiny", "input_tokens": 100}
    out = apply_system_prompt_override(env, None)
    assert out["input_tokens"] == 100
    assert out is not env  # shallow copy, never the original


def test_override_floors_at_one_token() -> None:
    env = {"system_prompt": "x" * 4000, "input_tokens": 10}
    out = apply_system_prompt_override(env, "")  # empty override => passthrough
    assert out["input_tokens"] == 10
    out2 = apply_system_prompt_override(env, "a")  # huge captured prompt, tiny override
    assert out2["input_tokens"] == 1


# --- endpoint tests ------------------------------------------------------------------

def test_estimate_empty_tenant(client: TestClient) -> None:
    r = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={"candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["samples_available"] == 0
    assert body["per_candidate"] == []
    assert body["diagnostics"]["prompt_override_applied"] is False


def test_estimate_rejects_missing_candidate(client: TestClient) -> None:
    r = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={"tenant_id": T, "sample_size": 10},
    )
    assert r.status_code == 422


def test_estimate_returns_single_candidate_projection(client: TestClient) -> None:
    _seed_samples(n=50)
    r = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "sample_size": 20,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["samples_available"] == 50
    assert len(body["per_candidate"]) == 1
    row = body["per_candidate"][0]
    assert row["model"] == "claude-haiku-4-5"
    assert row["samples_replayed"] > 0
    assert row["projected_monthly_cost_micro_usd"] > 0
    assert body["diagnostics"]["prompt_override_applied"] is False


def test_estimate_prompt_override_raises_projected_cost(client: TestClient) -> None:
    """A longer system prompt should raise the projected cost vs. no override."""
    _seed_samples(n=40, system_prompt="hi")  # tiny captured prompt
    base = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "sample_size": 20,
        },
    ).json()
    overridden = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "system_prompt_override": "a much longer system prompt " * 200,
            "sample_size": 20,
        },
    ).json()
    assert overridden["diagnostics"]["prompt_override_applied"] is True
    assert (
        overridden["per_candidate"][0]["projected_monthly_cost_micro_usd"]
        > base["per_candidate"][0]["projected_monthly_cost_micro_usd"]
    )


def test_estimate_uses_injected_candidate_client(client: TestClient) -> None:
    """The candidate client is injectable just like /v1/replay (no real provider call)."""
    seen: list[CandidateCall] = []

    async def fake_client(call: CandidateCall) -> CandidateResponse:
        seen.append(call)
        return CandidateResponse(input_tokens=call.envelope.get("input_tokens", 100),
                                 output_tokens=10, status_code=200)

    app.state.replay_candidate_client = fake_client
    _seed_samples(n=10, system_prompt="hi")
    r = client.post(
        "/v1/replay/estimate",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "system_prompt_override": "x" * 2000,
            "sample_size": 10,
        },
    )
    assert r.status_code == 200
    # The override must have reached the injected client's envelope.
    assert seen
    assert all(c.envelope.get("system_prompt") == "x" * 2000 for c in seen)
