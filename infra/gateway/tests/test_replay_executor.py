"""Replay executor — budget cap, concurrency, and write-through tests (CTO-113)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4


from tally.pricing import seed_catalog

from gateway.replay_executor import (
    CandidateCall,
    CandidateResponse,
    MAX_CONCURRENT_PER_TENANT,
    ReplayExecutor,
)
from gateway.replay_store import (
    InMemoryReplayBlobStore,
    ReplayRunRow,
    build_replay_object_key,
)


def _seed_blob(store: InMemoryReplayBlobStore, tenant: str, sample_id, *, input_tokens=100, output_tokens=50):
    key = build_replay_object_key(tenant, sample_id, datetime.now(timezone.utc))
    body = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens}).encode()
    store.put_bytes(key, body)
    return key


def _make_executor(*, client=None, todays_spend=lambda _t: 0, sink=None):
    sink = sink if sink is not None else []
    if isinstance(sink, list):
        sink_fn = sink.append
        rows_ref = sink
    else:
        sink_fn = sink
        rows_ref = None

    async def default_client(call: CandidateCall) -> CandidateResponse:
        env = call.envelope
        return CandidateResponse(
            input_tokens=int(env.get("input_tokens", 100)),
            output_tokens=int(env.get("output_tokens", 50)),
            status_code=200,
        )

    exec_ = ReplayExecutor(
        catalog=seed_catalog(),
        blob_store=InMemoryReplayBlobStore(),
        client=client or default_client,
        todays_spend_micro_usd=todays_spend,
        sink=sink_fn,
    )
    return exec_, rows_ref


# --- Happy path ---------------------------------------------------------------

def test_replay_writes_run_row() -> None:
    exec_, rows = _make_executor()
    sid = uuid4()
    key = _seed_blob(exec_.blob_store, "t1", sid, input_tokens=200, output_tokens=80)

    result = asyncio.run(exec_.replay_sample(
        tenant_id="t1",
        sample_id=sid,
        object_key=key,
        candidate_provider="anthropic",
        candidate_model="claude-haiku-4-5",
        daily_budget_usd=Decimal("5.00"),
    ))

    assert result.succeeded
    assert result.excluded_budget is False
    assert len(rows) == 1
    row: ReplayRunRow = rows[0]
    assert row.tenant_id == "t1"
    assert row.sample_id == sid
    assert row.candidate_provider == "anthropic"
    assert row.candidate_model == "claude-haiku-4-5"
    assert row.input_tokens == 200
    assert row.output_tokens == 80
    # Authoritative cost from the SDK catalog — non-zero for a known model.
    assert row.cost_micro_usd > 0


# --- Budget cap ---------------------------------------------------------------

def test_replay_skips_when_budget_exceeded() -> None:
    # Tenant has already spent $4.99 today; cap is $5; next call's projected cost is $0.05 →
    # over the cap → skipped with excluded_budget=True.
    already_spent = 4_990_000
    exec_, rows = _make_executor(todays_spend=lambda _t: already_spent)
    sid = uuid4()
    key = _seed_blob(exec_.blob_store, "t1", sid)

    result = asyncio.run(exec_.replay_sample(
        tenant_id="t1",
        sample_id=sid,
        object_key=key,
        candidate_provider="anthropic",
        candidate_model="claude-haiku-4-5",
        daily_budget_usd=Decimal("5.00"),
        estimated_call_cost_micro_usd=50_000,
    ))

    assert result.excluded_budget is True
    assert result.row is None
    assert rows == []


def test_replay_proceeds_when_within_budget() -> None:
    exec_, rows = _make_executor(todays_spend=lambda _t: 1_000_000)  # $1 spent
    sid = uuid4()
    key = _seed_blob(exec_.blob_store, "t1", sid)
    result = asyncio.run(exec_.replay_sample(
        tenant_id="t1", sample_id=sid, object_key=key,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        daily_budget_usd=Decimal("5.00"),
        estimated_call_cost_micro_usd=10_000,
    ))
    assert result.excluded_budget is False
    assert len(rows) == 1


# --- Concurrency --------------------------------------------------------------

def test_replay_respects_per_tenant_concurrency_limit() -> None:
    """10 concurrent replays for the same tenant — at most MAX_CONCURRENT_PER_TENANT run at once."""
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_client(call: CandidateCall) -> CandidateResponse:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return CandidateResponse(input_tokens=100, output_tokens=50)

    exec_, rows = _make_executor(client=slow_client)
    sids = [uuid4() for _ in range(10)]
    keys = [_seed_blob(exec_.blob_store, "t1", s) for s in sids]

    async def run_all():
        coros = [
            exec_.replay_sample(
                tenant_id="t1", sample_id=sids[i], object_key=keys[i],
                candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
                daily_budget_usd=Decimal("100.00"),
            )
            for i in range(10)
        ]
        return await asyncio.gather(*coros)

    results = asyncio.run(run_all())
    assert all(r.succeeded for r in results)
    assert len(rows) == 10
    assert peak <= MAX_CONCURRENT_PER_TENANT


# --- Retry --------------------------------------------------------------------

def test_replay_retries_on_transient_then_succeeds() -> None:
    calls = {"n": 0}

    async def flaky_client(call: CandidateCall) -> CandidateResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return CandidateResponse(
                input_tokens=0, output_tokens=0, status_code=503, error_msg="upstream",
            )
        return CandidateResponse(input_tokens=100, output_tokens=50, status_code=200)

    exec_, rows = _make_executor(client=flaky_client)
    sid = uuid4()
    key = _seed_blob(exec_.blob_store, "t1", sid)
    result = asyncio.run(exec_.replay_sample(
        tenant_id="t1", sample_id=sid, object_key=key,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        daily_budget_usd=Decimal("5.00"),
    ))
    assert calls["n"] == 2  # one retry
    assert result.succeeded


def test_replay_does_not_retry_on_4xx() -> None:
    calls = {"n": 0}

    async def bad_client(call: CandidateCall) -> CandidateResponse:
        calls["n"] += 1
        return CandidateResponse(
            input_tokens=0, output_tokens=0, status_code=400, error_msg="bad request",
        )

    exec_, rows = _make_executor(client=bad_client)
    sid = uuid4()
    key = _seed_blob(exec_.blob_store, "t1", sid)
    asyncio.run(exec_.replay_sample(
        tenant_id="t1", sample_id=sid, object_key=key,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        daily_budget_usd=Decimal("5.00"),
    ))
    assert calls["n"] == 1  # no retry
    # Row still written so the projection sees the 4xx as an error sample.
    assert len(rows) == 1
    assert rows[0].error_msg == "bad request"
