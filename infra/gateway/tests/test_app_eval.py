"""End-to-end tests for /v1/eval + /v1/tenant/eval/config (CTO-114)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.eval_executor import JudgeCall, JudgeResponse
from gateway.replay_store import (
    InMemoryReplayBlobStore,
    ReplayRunRow,
    ReplaySampleRow,
    build_replay_object_key,
)
from gateway.tenant_eval import EvalConfig
from gateway.tenant_replay import ReplayConfig

T = "t-acme"


class FakeReplayStore:
    def __init__(self) -> None:
        self._cfg: dict[str, ReplayConfig] = {}

    def get(self, tenant_id: str) -> ReplayConfig:
        return self._cfg.get(
            tenant_id,
            ReplayConfig(enabled=False, sample_rate=0.05, retention_days=30,
                         daily_budget_usd=Decimal("5.00")),
        )

    def upsert(self, tenant_id: str, **_kwargs) -> ReplayConfig:
        return self.get(tenant_id)


class FakeEvalStore:
    def __init__(self) -> None:
        self._cfg: dict[str, EvalConfig] = {}

    def get(self, tenant_id: str) -> EvalConfig:
        return self._cfg.get(
            tenant_id,
            EvalConfig(
                enabled=False, judge_model="claude-opus-4-8",
                daily_budget_usd=Decimal("10.00"),
            ),
        )

    def upsert(self, tenant_id: str, **kwargs) -> EvalConfig:
        current = self.get(tenant_id)
        new = EvalConfig(
            enabled=current.enabled if kwargs.get("enabled") is None else bool(kwargs["enabled"]),
            judge_model=current.judge_model if kwargs.get("judge_model") is None
                else str(kwargs["judge_model"]),
            daily_budget_usd=current.daily_budget_usd if kwargs.get("daily_budget_usd") is None
                else Decimal(str(kwargs["daily_budget_usd"])),
        )
        if new.daily_budget_usd < 0:
            raise ValueError("daily_budget_usd must be non-negative")
        if not new.judge_model:
            raise ValueError("judge_model must be non-empty")
        self._cfg[tenant_id] = new
        return new


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_replay = FakeReplayStore()
        app.state.tenant_eval = FakeEvalStore()
        app.state.replay_blob_store = InMemoryReplayBlobStore()
        app.state.replay_sample_index = []
        app.state.replay_runs = []
        app.state.eval_runs = []
        for attr in ("replay_candidate_client", "eval_judge_client"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
        yield c


def _seed_samples_and_runs(
    n: int, *, feature_tag: str = "chat",
    candidate_provider: str = "anthropic",
    candidate_model: str = "claude-haiku-4-5",
) -> None:
    """Pre-load n samples + replay_runs so /v1/eval has pairs to judge."""
    blob_store = app.state.replay_blob_store
    sample_index = app.state.replay_sample_index
    replay_runs = app.state.replay_runs
    now = datetime.now(timezone.utc)
    for i in range(n):
        sid = uuid4()
        key = build_replay_object_key(T, sid, now)
        body = json.dumps({
            "prompt": f"What is {i} + {i}?",
            "response": f"{i + i}",
            "candidate_response": f"The answer is {i + i}.",
            "input_tokens": 50 + i,
            "output_tokens": 20 + i,
        }).encode()
        blob_store.put_bytes(key, body)
        sample_index.append(ReplaySampleRow(
            tenant_id=T, sample_id=sid, trace_id=f"trace-{i}", feature_tag=feature_tag,
            real_provider="anthropic", real_model="claude-sonnet-4-5",
            input_tokens=50 + i, output_tokens=20 + i,
            captured_at=now, s3_object_key=key, pii_scrubbed=True,
        ))
        replay_runs.append(ReplayRunRow(
            tenant_id=T, run_id=uuid4(), sample_id=sid,
            candidate_provider=candidate_provider, candidate_model=candidate_model,
            input_tokens=50 + i, output_tokens=20 + i,
            cost_micro_usd=1234, latency_ms=42, error_msg="", ran_at=now,
        ))


# --- Config endpoints -------------------------------------------------------------

def test_eval_config_defaults_off(client: TestClient) -> None:
    r = client.get("/v1/tenant/eval/config", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    assert r.json()["config"]["enabled"] is False
    assert r.json()["config"]["judge_model"] == "claude-opus-4-8"
    assert r.json()["config"]["daily_budget_usd"] == 10.0


def test_eval_config_round_trip(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/eval/config",
        headers={"X-Tenant-Id": T},
        json={"enabled": True, "judge_model": "claude-sonnet-4-5", "daily_budget_usd": 25.0},
    )
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["enabled"] is True
    assert cfg["judge_model"] == "claude-sonnet-4-5"
    assert cfg["daily_budget_usd"] == 25.0


def test_eval_config_rejects_negative_budget(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/eval/config",
        headers={"X-Tenant-Id": T},
        json={"daily_budget_usd": -1.0},
    )
    assert r.status_code == 422


# --- Projection endpoint ----------------------------------------------------------

def test_eval_empty_tenant(client: TestClient) -> None:
    r = client.post(
        "/v1/eval",
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


def test_eval_returns_per_candidate_with_winrate_and_ci(client: TestClient) -> None:
    _seed_samples_and_runs(n=30)
    # Force a deterministic judge that always picks the candidate (verdict letter depends on
    # A/B placement, so emit literal "A" half the time and "B" half — but simplest: emit
    # nondeterministic letter via "TIE" so all rows are tie verdicts).
    async def tie_judge(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(text="TIE", input_tokens=200, output_tokens=2)
    app.state.eval_judge_client = tie_judge

    r = client.post(
        "/v1/eval",
        headers={"X-Tenant-Id": T},
        json={
            "tenant_id": T,
            "candidate_models": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
            ],
            "sample_size": 30,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["samples_available"] == 30
    assert len(body["per_candidate"]) == 1
    cand = body["per_candidate"][0]
    # 30 judged ties → 0 candidate wins → win_rate 0; both CI bounds at the Wilson [0, hi] end.
    assert cand["samples_judged"] == 30
    assert cand["ties"] == 30
    assert cand["win_rate"] == 0.0
    assert 0.0 <= cand["win_rate_ci_lo"] <= cand["win_rate_ci_hi"] <= 1.0
    assert cand["judge_cost_micro_usd"] > 0
    # Diagnostics surface the judge config.
    assert body["diagnostics"]["judge_model"] == "claude-opus-4-8"
    assert body["diagnostics"]["rubric_version"] == "rubric-v1"


def test_eval_candidate_wins_aggregate(client: TestClient) -> None:
    """Judge always picks 'A'. With A/B randomized 50/50, ~half should resolve to candidate_wins."""
    _seed_samples_and_runs(n=40)

    async def a_judge(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(text="A", input_tokens=200, output_tokens=2)
    app.state.eval_judge_client = a_judge

    r = client.post(
        "/v1/eval",
        headers={"X-Tenant-Id": T},
        json={"tenant_id": T,
              "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4-5"}],
              "sample_size": 40},
    )
    body = r.json()
    cand = body["per_candidate"][0]
    # Sanity: both verdicts must appear (i.e. A/B was actually randomized — not all candidate_wins
    # nor all current_wins). The split won't be exactly 20/20 but it should be in a wide band.
    assert cand["candidate_wins"] > 5
    assert cand["current_wins"] > 5
    assert cand["candidate_wins"] + cand["current_wins"] == 40
    assert cand["ties"] == 0


# --- CTO-125: judge reads persisted candidate response text ----------------------

def _seed_with_persisted_response(
    *, persisted_text: str, envelope_candidate: str, feature_tag: str = "chat",
) -> None:
    """One sample whose envelope carries a *different* candidate_response than the persisted
    replay_run.response_text, so a test can tell which one the judge actually graded."""
    blob_store = app.state.replay_blob_store
    sample_index = app.state.replay_sample_index
    replay_runs = app.state.replay_runs
    now = datetime.now(timezone.utc)
    sid = uuid4()
    key = build_replay_object_key(T, sid, now)
    body = json.dumps({
        "prompt": "What is 2 + 2?",
        "response": "4",
        "candidate_response": envelope_candidate,
        "input_tokens": 50,
        "output_tokens": 20,
    }).encode()
    blob_store.put_bytes(key, body)
    sample_index.append(ReplaySampleRow(
        tenant_id=T, sample_id=sid, trace_id="trace-x", feature_tag=feature_tag,
        real_provider="anthropic", real_model="claude-sonnet-4-5",
        input_tokens=50, output_tokens=20,
        captured_at=now, s3_object_key=key, pii_scrubbed=True,
    ))
    replay_runs.append(ReplayRunRow(
        tenant_id=T, run_id=uuid4(), sample_id=sid,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        input_tokens=50, output_tokens=20,
        cost_micro_usd=1234, latency_ms=42, error_msg="", ran_at=now,
        response_text=persisted_text, finish_reason="stop",
    ))


def test_eval_judges_persisted_response_not_envelope(client: TestClient) -> None:
    """The judge must grade the persisted replay_run.response_text, NOT the envelope re-render."""
    _seed_with_persisted_response(
        persisted_text="PERSISTED candidate answer",
        envelope_candidate="ENVELOPE reconstructed answer",
    )
    seen_prompts: list[str] = []

    async def capturing_judge(call: JudgeCall) -> JudgeResponse:
        seen_prompts.append(call.prompt)
        return JudgeResponse(text="TIE", input_tokens=100, output_tokens=2)
    app.state.eval_judge_client = capturing_judge

    r = client.post(
        "/v1/eval",
        headers={"X-Tenant-Id": T},
        json={"tenant_id": T,
              "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4-5"}],
              "sample_size": 1},
    )
    assert r.status_code == 200
    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    assert "PERSISTED candidate answer" in prompt
    assert "ENVELOPE reconstructed answer" not in prompt


def test_eval_legacy_row_falls_back_to_envelope(client: TestClient) -> None:
    """Legacy replay_runs predating CTO-125 have empty response_text → judge falls back to the
    envelope reconstruct path so historical eval results keep working."""
    _seed_with_persisted_response(
        persisted_text="",  # legacy row: column absent / empty
        envelope_candidate="ENVELOPE reconstructed answer",
    )
    seen_prompts: list[str] = []

    async def capturing_judge(call: JudgeCall) -> JudgeResponse:
        seen_prompts.append(call.prompt)
        return JudgeResponse(text="TIE", input_tokens=100, output_tokens=2)
    app.state.eval_judge_client = capturing_judge

    r = client.post(
        "/v1/eval",
        headers={"X-Tenant-Id": T},
        json={"tenant_id": T,
              "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4-5"}],
              "sample_size": 1},
    )
    assert r.status_code == 200
    assert len(seen_prompts) == 1
    assert "ENVELOPE reconstructed answer" in seen_prompts[0]
