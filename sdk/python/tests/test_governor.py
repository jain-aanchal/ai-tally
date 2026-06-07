# SPDX-License-Identifier: Apache-2.0
import random

from tally.governor import (
    DEFAULT_MAX_CONCURRENCY,
    Admission,
    Decision,
    GovernorConfig,
    GovernorDiagnostics,
    Outcome,
    RateLimitGovernor,
    backoff_delay,
    counts_toward_metrics,
    is_throttled,
)

# --- throttle classification ---------------------------------------------------------------------


def test_is_throttled_by_status():
    assert is_throttled(status=429) is True
    assert is_throttled(status=200) is False
    assert is_throttled(status=500) is False


def test_is_throttled_by_signal_case_insensitive():
    assert is_throttled(signal="throttled") is True
    assert is_throttled(signal="RATE_LIMIT") is True
    assert is_throttled(signal="  Rate_Limited ") is True
    assert is_throttled(signal="ok") is False


def test_is_throttled_defensive_on_garbage():
    assert is_throttled() is False
    assert is_throttled(status=None, signal=None) is False


# --- metrics exclusion rule ----------------------------------------------------------------------


def test_counts_toward_metrics_only_completed():
    assert counts_toward_metrics(Outcome.COMPLETED) is True
    assert counts_toward_metrics(Outcome.THROTTLED) is False
    assert counts_toward_metrics(Outcome.EXCLUDED) is False
    # still in flight → no result yet
    assert counts_toward_metrics(Outcome.ADMITTED) is False


def test_classify_maps_429_to_throttled():
    gov = RateLimitGovernor()
    assert gov.classify(status=429) is Outcome.THROTTLED
    assert gov.classify(signal="rate_limit") is Outcome.THROTTLED
    assert gov.classify(status=200) is Outcome.COMPLETED


# --- backoff with injectable jitter --------------------------------------------------------------


def test_backoff_full_jitter_bounds():
    # rand=1.0-ish would equal the cap; full jitter keeps it in [0, capped).
    for attempt in range(6):
        capped = min(60.0, 0.5 * 2**attempt)
        d = backoff_delay(attempt, rand=lambda: 0.999999)
        assert 0.0 <= d < capped + 1e-9
        assert d <= capped


def test_backoff_zero_jitter_is_zero():
    assert backoff_delay(5, rand=lambda: 0.0) == 0.0


def test_backoff_grows_on_repeated_429():
    # With a fixed jitter factor, the cap (and thus delay) grows monotonically per attempt.
    rand = lambda: 0.5  # noqa: E731
    delays = [backoff_delay(a, rand=rand) for a in range(6)]
    for earlier, later in zip(delays, delays[1:], strict=False):
        assert later >= earlier
    assert delays[-1] > delays[0]


def test_backoff_clamped_to_max():
    d = backoff_delay(100, base_s=1.0, max_s=10.0, rand=lambda: 0.9999)
    assert d <= 10.0


def test_backoff_negative_attempt_clamped():
    assert backoff_delay(-5, rand=lambda: 0.0) == 0.0


def test_backoff_deterministic_with_seed():
    r1 = random.Random(42).random
    r2 = random.Random(42).random
    assert backoff_delay(3, rand=r1) == backoff_delay(3, rand=r2)


def test_backoff_defensive_on_bad_rand():
    # An injected callable that violates [0,1) is coerced to 0 rather than producing garbage.
    assert backoff_delay(3, rand=lambda: 5.0) == 0.0
    assert backoff_delay(3, rand=lambda: -1.0) == 0.0


# --- config & per-provider caps ------------------------------------------------------------------


def test_cap_for_known_and_unknown_provider():
    cfg = GovernorConfig(max_concurrency={"openai": 4, "anthropic": 2})
    assert cfg.cap_for("openai") == 4
    assert cfg.cap_for("anthropic") == 2
    # unknown provider falls back to default
    assert cfg.cap_for("mistral") == DEFAULT_MAX_CONCURRENCY


def test_cap_for_floors_at_one_and_handles_garbage():
    cfg = GovernorConfig(max_concurrency={"a": 0, "b": -3, "c": "x"})  # type: ignore[dict-item]
    # 0 and negatives floor at 1 (a cap below 1 would deadlock the gate)
    assert cfg.cap_for("a") == 1
    assert cfg.cap_for("b") == 1
    # an unparseable value degrades gracefully to the default cap
    assert cfg.cap_for("c") == DEFAULT_MAX_CONCURRENCY


# --- admission / concurrency gate ----------------------------------------------------------------


def test_admit_until_cap_then_wait():
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": 2}))
    a1 = gov.admit("openai")
    a2 = gov.admit("openai")
    a3 = gov.admit("openai")
    assert a1.decision is Decision.ADMIT and a1.admitted
    assert a2.decision is Decision.ADMIT
    assert a3.decision is Decision.WAIT and not a3.admitted
    assert gov.in_flight("openai") == 2


def test_release_frees_slot_and_admits_again():
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": 1}))
    assert gov.admit("openai").admitted
    assert not gov.admit("openai").admitted
    gov.release("openai")
    assert gov.admit("openai").admitted


def test_release_never_goes_below_zero():
    gov = RateLimitGovernor()
    gov.release("openai")
    gov.release("openai")
    assert gov.in_flight("openai") == 0


def test_would_admit_is_pure():
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": 1}))
    assert gov.would_admit("openai") is True
    # pure check did not consume a slot
    assert gov.in_flight("openai") == 0
    gov.admit("openai")
    assert gov.would_admit("openai") is False


def test_admission_is_immutable_snapshot():
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": 4}))
    a = gov.admit("openai")
    assert isinstance(a, Admission)
    assert a.provider == "openai" and a.cap == 4 and a.in_flight == 1


# --- diagnostics ---------------------------------------------------------------------------------


def test_diagnostics_accumulates_per_provider_and_totals():
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": 5, "anthropic": 5}))
    gov.admit("openai")
    gov.record_completed("openai")
    gov.record_throttle("openai")
    gov.record_excluded("openai")
    gov.record_retry("openai")
    gov.admit("anthropic")
    gov.record_throttle("anthropic")

    diag = gov.diagnostics()
    assert isinstance(diag, GovernorDiagnostics)
    assert diag.total_admitted == 2
    assert diag.total_completed == 1
    assert diag.total_throttled == 2
    assert diag.total_excluded == 1
    assert diag.total_retried == 1
    assert diag.total_in_flight == 2

    by_provider = {p.provider: p for p in diag.providers}
    assert by_provider["openai"].throttled == 1
    assert by_provider["anthropic"].throttled == 1


def test_diagnostics_as_dict_is_json_shaped():
    gov = RateLimitGovernor()
    gov.admit("openai")
    gov.record_throttle("openai")
    d = gov.diagnostics().as_dict()
    import json

    json.dumps(d)  # must be JSON-serializable
    assert d["total_throttled"] == 1
    assert isinstance(d["providers"], list)
    assert d["providers"][0]["provider"] == "openai"


# --- no-bans-under-sustained-load simulation -----------------------------------------------------


def test_simulated_sustained_load_never_exceeds_cap_and_backoff_grows():
    """Deterministic simulated replay: cap is never breached and backoff grows on repeated 429s."""
    cap = 3
    gov = RateLimitGovernor(GovernorConfig(max_concurrency={"openai": cap}))
    rng = random.Random(7)
    in_flight_ids: list[int] = []
    next_id = 0
    max_observed = 0
    throttle_attempt = 0
    backoffs: list[float] = []

    # 500 synthetic ticks: try to admit, sometimes a 429, sometimes complete.
    for _ in range(500):
        # try to start new work
        adm = gov.admit("openai")
        if adm.admitted:
            in_flight_ids.append(next_id)
            next_id += 1
        max_observed = max(max_observed, gov.in_flight("openai"))
        assert gov.in_flight("openai") <= cap  # invariant: never exceed cap

        if not in_flight_ids:
            continue

        roll = rng.random()
        if roll < 0.25:
            # provider throttles us → record, schedule growing backoff, release the slot
            gov.record_throttle("openai")
            gov.record_retry("openai")
            backoffs.append(gov.backoff_delay(throttle_attempt, rand=lambda: 0.5))
            throttle_attempt += 1
            in_flight_ids.pop()
            gov.release("openai")
        else:
            # clean completion
            gov.record_completed("openai")
            in_flight_ids.pop()
            gov.release("openai")

    assert max_observed <= cap
    assert gov.in_flight("openai") <= cap
    # backoff schedule grew over the first several throttles (until clamped at max_delay)
    assert len(backoffs) >= 3
    assert backoffs[2] > backoffs[0]
    # throttled calls were tallied and are excluded from metrics by rule
    diag = gov.diagnostics()
    assert diag.total_throttled > 0
    assert counts_toward_metrics(Outcome.THROTTLED) is False
