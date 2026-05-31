"""Pure tests for the backpressure controller + retry classification (CTO-36). No infra."""

from __future__ import annotations

from gateway.backpressure import Backpressure, is_retryable


def test_healthy_below_soft_limit_admits_all() -> None:
    bp = Backpressure(soft_limit=10)
    shed = bp.evaluate(in_flight=3, batch_items=500)
    assert shed.overloaded is False
    assert shed.keep == 500
    assert shed.hints.sample_rate_override is None
    assert shed.hints.retry_after_ms == 0


def test_overloaded_at_soft_limit_tightens_and_sheds() -> None:
    bp = Backpressure(soft_limit=10, overload_max_batch=100)
    shed = bp.evaluate(in_flight=10, batch_items=500)
    assert shed.overloaded is True
    assert shed.keep == 100  # capped to the tightened ceiling
    assert shed.hints.max_batch_size == 100
    assert shed.hints.sample_rate_override == 0.25
    assert shed.hints.retry_after_ms > 0
    assert shed.hints.flush_interval_ms > bp.healthy_hints().flush_interval_ms


def test_overloaded_small_batch_keeps_all_but_still_advises() -> None:
    bp = Backpressure(soft_limit=10, overload_max_batch=100)
    shed = bp.evaluate(in_flight=20, batch_items=40)
    assert shed.overloaded is True
    assert shed.keep == 40  # nothing to shed, but hints still tightened
    assert shed.hints.sample_rate_override == 0.25


def test_soft_limit_must_be_positive() -> None:
    try:
        Backpressure(soft_limit=0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_is_retryable_classification() -> None:
    assert is_retryable(429) is True
    assert is_retryable(503) is True
    assert is_retryable(500) is True
    assert is_retryable(502) is True
    # 4xx contract errors (other than 429) are terminal — retrying replays the rejection.
    assert is_retryable(400) is False
    assert is_retryable(401) is False
    assert is_retryable(403) is False
    assert is_retryable(422) is False
    assert is_retryable(200) is False
