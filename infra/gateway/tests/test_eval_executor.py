"""Eval executor — judge call, budget cap, position-bias mitigation, concurrency (CTO-114)."""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from uuid import uuid4


from tally.pricing import seed_catalog

from gateway.eval_executor import (
    MAX_CONCURRENT_PER_TENANT,
    EvalExecutor,
    JudgeCall,
    JudgeResponse,
    parse_verdict,
)
from gateway.eval_store import (
    VERDICT_CANDIDATE_WINS,
    VERDICT_CURRENT_WINS,
    VERDICT_ERROR,
    VERDICT_TIE,
)


def _make_executor(*, client=None, todays_spend=lambda _t: 0, rng_seed: int = 0, sink=None):
    sink = sink if sink is not None else []
    if isinstance(sink, list):
        sink_fn = sink.append
        rows_ref = sink
    else:
        sink_fn = sink
        rows_ref = None

    async def default_client(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(
            text="TIE", input_tokens=100, output_tokens=2, status_code=200,
        )

    exec_ = EvalExecutor(
        catalog=seed_catalog(),
        judge_client=client or default_client,
        todays_spend_micro_usd=todays_spend,
        sink=sink_fn,
        rng=random.Random(rng_seed),
    )
    return exec_, rows_ref


# --- parse_verdict --------------------------------------------------------------

def test_parse_verdict_letter_a_candidate_on_a() -> None:
    # Judge said "A", and A was the candidate → candidate wins.
    assert parse_verdict("A", a_is_candidate=True) == VERDICT_CANDIDATE_WINS
    assert parse_verdict("A", a_is_candidate=False) == VERDICT_CURRENT_WINS


def test_parse_verdict_letter_b() -> None:
    assert parse_verdict("B", a_is_candidate=True) == VERDICT_CURRENT_WINS
    assert parse_verdict("B", a_is_candidate=False) == VERDICT_CANDIDATE_WINS


def test_parse_verdict_tie_regardless_of_order() -> None:
    assert parse_verdict("TIE", a_is_candidate=True) == VERDICT_TIE
    assert parse_verdict("TIE", a_is_candidate=False) == VERDICT_TIE


def test_parse_verdict_unparseable_returns_none() -> None:
    assert parse_verdict("", a_is_candidate=True) is None
    assert parse_verdict("I think they're both fine.", a_is_candidate=True) is None


# --- Happy path -----------------------------------------------------------------

def test_judge_pair_writes_row() -> None:
    exec_, rows = _make_executor()
    sid = uuid4()
    rid = uuid4()
    result = asyncio.run(exec_.judge_pair(
        tenant_id="t1", replay_run_id=rid, sample_id=sid,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        instruction="What is 2+2?",
        current_response="4",
        candidate_response="The answer is 4.",
        daily_budget_usd=Decimal("10.00"),
    ))
    assert result.succeeded
    assert result.verdict == VERDICT_TIE
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == "t1"
    assert row.judge_verdict == VERDICT_TIE
    assert row.judge_model == "claude-opus-4-8"
    assert row.judge_prompt_version == "rubric-v1"
    assert row.cost_micro_usd > 0  # opus is priced; non-zero


# --- Budget cap -----------------------------------------------------------------

def test_judge_pair_skipped_when_budget_exceeded() -> None:
    # $9.99 already spent of $10 cap; projected $0.05 → over → skip.
    exec_, rows = _make_executor(todays_spend=lambda _t: 9_990_000)
    sid, rid = uuid4(), uuid4()
    result = asyncio.run(exec_.judge_pair(
        tenant_id="t1", replay_run_id=rid, sample_id=sid,
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        instruction="x", current_response="y", candidate_response="z",
        daily_budget_usd=Decimal("10.00"),
        estimated_call_cost_micro_usd=50_000,
    ))
    assert result.excluded_budget is True
    assert result.row is None
    assert rows == []


# --- Position-bias mitigation ---------------------------------------------------

def test_position_bias_a_b_randomized_per_call() -> None:
    """Over many calls A and B should each carry the candidate roughly half the time.

    We expose this via the judge_client: the prompt contains "RESPONSE A:\n<text>" — when the
    candidate's marker text appears on the A side, that's an "a_is_candidate" call. Across N
    calls with a fixed PRNG, we should see *both* placements (not all-A or all-B).
    """
    a_is_candidate_count = {"true": 0, "false": 0}

    async def inspector(call: JudgeCall) -> JudgeResponse:
        # Find which response is "CANDIDATE_MARKER" — that side is the candidate.
        # RESPONSE A: appears once in the prompt; just look at what follows it.
        a_idx = call.prompt.index("RESPONSE A:")
        b_idx = call.prompt.index("RESPONSE B:")
        a_text = call.prompt[a_idx:b_idx]
        if "CANDIDATE_MARKER" in a_text:
            a_is_candidate_count["true"] += 1
        else:
            a_is_candidate_count["false"] += 1
        return JudgeResponse(text="TIE", input_tokens=100, output_tokens=2)

    exec_, _ = _make_executor(client=inspector, rng_seed=12345)

    async def run_many():
        for _ in range(40):
            await exec_.judge_pair(
                tenant_id="t1", replay_run_id=uuid4(), sample_id=uuid4(),
                candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
                instruction="x",
                current_response="CURRENT_MARKER",
                candidate_response="CANDIDATE_MARKER",
                daily_budget_usd=Decimal("100.00"),
            )

    asyncio.run(run_many())
    # Both sides must be picked at least a few times — if A/B placement were fixed, one count
    # would be zero. With seed=12345 and 40 calls we expect roughly 20/20.
    assert a_is_candidate_count["true"] > 5
    assert a_is_candidate_count["false"] > 5
    # And they should sum to 40 — no calls dropped.
    assert a_is_candidate_count["true"] + a_is_candidate_count["false"] == 40


def test_position_bias_verdict_decoded_correctly_when_a_is_candidate() -> None:
    """If a_is_candidate AND judge says A → candidate_wins. The executor must wire this through."""
    # Force a_is_candidate=True by seeding rng so first random() < 0.5.
    seed = 0
    while True:
        r = random.Random(seed)
        if r.random() < 0.5:
            break
        seed += 1

    async def always_a(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(text="A", input_tokens=10, output_tokens=1)

    exec_, _ = _make_executor(client=always_a, rng_seed=seed)
    result = asyncio.run(exec_.judge_pair(
        tenant_id="t1", replay_run_id=uuid4(), sample_id=uuid4(),
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        instruction="x", current_response="cur", candidate_response="cand",
        daily_budget_usd=Decimal("10.00"),
    ))
    assert result.verdict == VERDICT_CANDIDATE_WINS


# --- Concurrency ----------------------------------------------------------------

def test_judge_pair_respects_per_tenant_concurrency_limit() -> None:
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow(call: JudgeCall) -> JudgeResponse:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return JudgeResponse(text="TIE", input_tokens=10, output_tokens=1)

    exec_, rows = _make_executor(client=slow)

    async def run_all():
        coros = [
            exec_.judge_pair(
                tenant_id="t1", replay_run_id=uuid4(), sample_id=uuid4(),
                candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
                instruction="x", current_response="c", candidate_response="d",
                daily_budget_usd=Decimal("100.00"),
            )
            for _ in range(10)
        ]
        return await asyncio.gather(*coros)

    results = asyncio.run(run_all())
    assert all(r.succeeded for r in results)
    assert len(rows) == 10
    assert peak <= MAX_CONCURRENT_PER_TENANT


# --- Error handling -------------------------------------------------------------

def test_judge_error_writes_error_row_not_silent_drop() -> None:
    async def boom(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(text="", input_tokens=0, output_tokens=0,
                             status_code=500, error_msg="upstream blew up")

    exec_, rows = _make_executor(client=boom)
    result = asyncio.run(exec_.judge_pair(
        tenant_id="t1", replay_run_id=uuid4(), sample_id=uuid4(),
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        instruction="x", current_response="c", candidate_response="d",
        daily_budget_usd=Decimal("10.00"),
    ))
    assert result.verdict == VERDICT_ERROR
    assert len(rows) == 1
    assert rows[0].judge_verdict == VERDICT_ERROR
    assert "upstream" in rows[0].error_msg


def test_unparseable_judge_output_becomes_error_row() -> None:
    async def chatty(call: JudgeCall) -> JudgeResponse:
        return JudgeResponse(text="Well, it's complicated... maybe?",
                             input_tokens=10, output_tokens=10)

    exec_, rows = _make_executor(client=chatty)
    result = asyncio.run(exec_.judge_pair(
        tenant_id="t1", replay_run_id=uuid4(), sample_id=uuid4(),
        candidate_provider="anthropic", candidate_model="claude-haiku-4-5",
        instruction="x", current_response="c", candidate_response="d",
        daily_budget_usd=Decimal("10.00"),
    ))
    assert result.verdict == VERDICT_ERROR
    assert len(rows) == 1
    assert "unparseable" in rows[0].error_msg
