"""The local MOCK judge distribution (CTO-237).

The dev/test judge used to always emit "TIE", which pinned every candidate win-rate to 0.0 and made
the replay-backed Compare/eval and the wrong-sized-model waste detector produce no usable signal.
It now derives a deterministic-but-balanced verdict from the prompt, so a candidate's pairwise
win-rate lands near 0.5 with a Wilson CI that overlaps 0.5 ("statistically indistinguishable").
"""

from __future__ import annotations

import asyncio

from gateway.app import _mock_judge_client, _wilson_interval
from gateway.eval_executor import JudgeCall


def _judge(prompt: str) -> str:
    resp = asyncio.run(_mock_judge_client(JudgeCall(provider="anthropic", model="m", prompt=prompt)))
    return resp.text


def test_mock_judge_is_deterministic() -> None:
    prompt = "INSTRUCTION: sum\nRESPONSE A: 4\nRESPONSE B: four"
    assert _judge(prompt) == _judge(prompt)


def test_mock_judge_distribution_is_non_degenerate() -> None:
    verdicts = [_judge(f"instruction {i}\nA: alpha {i}\nB: beta {i}") for i in range(400)]
    counts = {v: verdicts.count(v) for v in ("A", "B", "TIE")}
    # Not all-TIE, and every bucket is actually populated.
    assert counts["A"] > 0
    assert counts["B"] > 0
    assert counts["TIE"] > 0
    assert counts["TIE"] < len(verdicts)
    # Roughly balanced A vs B (target ~47.5/47.5/5). Wide band keeps the test stable.
    assert 0.40 < counts["A"] / len(verdicts) < 0.55
    assert 0.40 < counts["B"] / len(verdicts) < 0.55
    assert counts["TIE"] / len(verdicts) < 0.15


def test_mock_judge_yields_winrate_indistinguishable_from_half() -> None:
    """The aggregate candidate win-rate is fair: candidate wins ~= current wins, CI overlaps 0.5.

    The executor randomizes A/B placement per sample (position-bias mitigation); here we model that
    deterministically by alternating which side carries the candidate, so the test has no RNG
    dependence. With a balanced A/B letter from the mock, candidate wins land level with current
    wins and the candidate win-rate's Wilson CI straddles 0.5 - exactly the "statistically
    indistinguishable quality" verdict the wrong-sized-model waste detector keys on. It must NOT
    be biased toward the candidate.
    """
    candidate_wins = current_wins = ties = 0
    for i in range(200):
        a_is_candidate = (i % 2 == 0)  # deterministic 50/50 position split
        cand, cur = f"cand-{i}", f"cur-{i}"
        resp_a = cand if a_is_candidate else cur
        resp_b = cur if a_is_candidate else cand
        letter = _judge(f"INSTRUCTION: q{i}\nRESPONSE A: {resp_a}\nRESPONSE B: {resp_b}")
        if letter == "TIE":
            ties += 1
        elif (letter == "A") == a_is_candidate:  # mirrors eval_executor.parse_verdict
            candidate_wins += 1
        else:
            current_wins += 1
    non_error = candidate_wins + current_wins + ties  # ties count in the denominator
    win_rate = candidate_wins / non_error
    lo, hi = _wilson_interval(candidate_wins, non_error)
    # Fair: neither side dominates, and win-rate sits near 0.5.
    assert abs(candidate_wins - current_wins) < 0.25 * non_error
    assert 0.40 < win_rate < 0.60
    # The whole point: quality is indistinguishable, so the CI must straddle 0.5.
    assert lo <= 0.5 <= hi
