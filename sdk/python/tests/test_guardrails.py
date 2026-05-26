import pytest

from tally.guardrails import (
    CostLimitExceededException,
    GuardrailConfig,
    GuardrailEngine,
    GuardrailState,
    Limit,
    Mode,
)


def test_under_limit_proceeds():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", cumulative_cost_micro_usd=100)
    cfg = GuardrailConfig(mode=Mode.GRACEFUL, max_cost_micro_usd=1000)
    v = eng.evaluate(state, cfg)
    assert v.proceed and v.breached is None


def test_observe_never_raises_but_counts():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", step_count=20)
    cfg = GuardrailConfig(mode=Mode.OBSERVE, max_steps=15)
    v = eng.evaluate(state, cfg)
    assert v.proceed is True
    assert v.would_fire is True
    assert v.breached is Limit.STEPS
    assert eng.would_fire_counts[Limit.STEPS] == 1


def test_graceful_raises_localized_exception():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="run-1", cumulative_cost_micro_usd=2000)
    cfg = GuardrailConfig(mode=Mode.GRACEFUL, max_cost_micro_usd=1000)
    with pytest.raises(CostLimitExceededException) as ei:
        eng.evaluate(state, cfg)
    assert ei.value.limit is Limit.COST
    assert ei.value.trace_id == "run-1"


def test_hard_stop_raises():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", tool_call_count=99)
    cfg = GuardrailConfig(mode=Mode.HARD_STOP, max_tool_calls=10)
    with pytest.raises(CostLimitExceededException):
        eng.evaluate(state, cfg)


def test_warn_proceeds_with_message():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", cumulative_cost_micro_usd=1500)
    cfg = GuardrailConfig(mode=Mode.WARN, max_cost_micro_usd=1000)
    v = eng.evaluate(state, cfg)
    assert v.proceed is True
    assert v.breached is Limit.COST
    assert "exceeded" in v.warning


def test_warn_near_threshold():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", step_count=8)
    cfg = GuardrailConfig(mode=Mode.WARN, max_steps=10, warn_at=0.8)
    v = eng.evaluate(state, cfg)
    assert v.proceed is True
    assert v.breached is None
    assert "converge" in v.warning


def test_cost_checked_first():
    eng = GuardrailEngine()
    state = GuardrailState(
        trace_id="t", cumulative_cost_micro_usd=5000, step_count=99, tool_call_count=99
    )
    cfg = GuardrailConfig(
        mode=Mode.GRACEFUL, max_cost_micro_usd=1000, max_steps=10, max_tool_calls=10
    )
    with pytest.raises(CostLimitExceededException) as ei:
        eng.evaluate(state, cfg)
    assert ei.value.limit is Limit.COST


def test_state_counters_accumulate():
    state = GuardrailState(trace_id="t")
    state.add_cost(100)
    state.add_cost(50)
    state.incr_step()
    state.incr_tool_call()
    state.incr_tool_call()
    assert state.cumulative_cost_micro_usd == 150
    assert state.step_count == 1
    assert state.tool_call_count == 2


def test_no_caps_always_proceeds():
    eng = GuardrailEngine()
    state = GuardrailState(trace_id="t", cumulative_cost_micro_usd=10**9)
    v = eng.evaluate(state, GuardrailConfig(mode=Mode.GRACEFUL))
    assert v.proceed is True
