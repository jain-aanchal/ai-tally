# SPDX-License-Identifier: Apache-2.0
"""Local guardrail engine — protect budget without ever corrupting customer state.

Implements CTO-51.

A per-trace counter state machine ``{cumulative_cost, step_count, tool_call_count}``. Before each
outbound LLM/tool call the engine is consulted. Enforcement is tiered and **never hard-kills by
default**:

- ``OBSERVE`` (default): record what *would* have fired; always proceed. Powers the "fires/wk"
  graduation metric in the UI (CTO-58).
- ``WARN``: proceed, but return a warning the agent can act on (e.g. inject "you have used 80% of
  budget; converge").
- ``GRACEFUL``: raise a localized :class:`CostLimitExceededException` that the agent framework
  catches, runs cleanup, and returns a degraded response. The process is **not** killed.
- ``HARD_STOP``: opt-in only, for idempotent/read-only agents.

v1 is single-process (counters are per-process). The metric that triggers v2 (a shared counter)
is ``agent_run.cross_process_ratio`` — see CTO-83.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    OBSERVE = "observe"
    WARN = "warn"
    GRACEFUL = "graceful"
    HARD_STOP = "hard_stop"


class Limit(str, Enum):
    COST = "cost"
    STEPS = "steps"
    TOOL_CALLS = "tool_calls"


class CostLimitExceededException(Exception):
    """Raised in GRACEFUL/HARD_STOP modes. Localized — meant to be caught by the agent framework
    so it can clean up and return a degraded response. Never a process kill."""

    def __init__(self, limit: Limit, value: float, cap: float, trace_id: str | None = None):
        self.limit = limit
        self.value = value
        self.cap = cap
        self.trace_id = trace_id
        super().__init__(
            f"guardrail '{limit.value}' exceeded: {value} > {cap}"
            + (f" (trace {trace_id})" if trace_id else "")
        )


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    mode: Mode = Mode.OBSERVE
    max_cost_micro_usd: int | None = None
    max_steps: int | None = None
    max_tool_calls: int | None = None
    #: fraction of a cap at which WARN mode starts warning (0.8 == 80%)
    warn_at: float = 0.8


@dataclass(slots=True)
class GuardrailState:
    trace_id: str
    cumulative_cost_micro_usd: int = 0
    step_count: int = 0
    tool_call_count: int = 0

    def add_cost(self, micro_usd: int) -> None:
        self.cumulative_cost_micro_usd += micro_usd

    def incr_step(self) -> None:
        self.step_count += 1

    def incr_tool_call(self) -> None:
        self.tool_call_count += 1


@dataclass(frozen=True, slots=True)
class Verdict:
    proceed: bool
    breached: Limit | None = None
    warning: str | None = None
    #: True when a breach occurred but mode was OBSERVE (would-have-fired)
    would_fire: bool = False


def _first_breach(state: GuardrailState, config: GuardrailConfig) -> tuple[Limit, int, int] | None:
    if config.max_cost_micro_usd is not None and (
        state.cumulative_cost_micro_usd > config.max_cost_micro_usd
    ):
        return Limit.COST, state.cumulative_cost_micro_usd, config.max_cost_micro_usd
    if config.max_steps is not None and state.step_count > config.max_steps:
        return Limit.STEPS, state.step_count, config.max_steps
    if config.max_tool_calls is not None and state.tool_call_count > config.max_tool_calls:
        return Limit.TOOL_CALLS, state.tool_call_count, config.max_tool_calls
    return None


def _near_breach(state: GuardrailState, config: GuardrailConfig) -> str | None:
    checks = [
        (config.max_cost_micro_usd, state.cumulative_cost_micro_usd, "cost"),
        (config.max_steps, state.step_count, "steps"),
        (config.max_tool_calls, state.tool_call_count, "tool_calls"),
    ]
    for cap, value, name in checks:
        if cap is not None and cap > 0 and value >= config.warn_at * cap:
            pct = round(100 * value / cap)
            return f"{name} at {pct}% of budget ({value}/{cap}); converge"
    return None


@dataclass(slots=True)
class GuardrailEngine:
    """Evaluates guardrails against per-trace state. Holds the OBSERVE-mode would-fire tally."""

    would_fire_counts: dict[Limit, int] = field(default_factory=dict)

    def evaluate(self, state: GuardrailState, config: GuardrailConfig) -> Verdict:
        """Consult the guardrail. Call BEFORE the next LLM/tool call.

        Raises :class:`CostLimitExceededException` only in GRACEFUL / HARD_STOP modes.
        """
        breach = _first_breach(state, config)

        if breach is None:
            warning = _near_breach(state, config) if config.mode == Mode.WARN else None
            return Verdict(proceed=True, warning=warning)

        limit, value, cap = breach

        if config.mode == Mode.OBSERVE:
            self.would_fire_counts[limit] = self.would_fire_counts.get(limit, 0) + 1
            return Verdict(proceed=True, breached=limit, would_fire=True)

        if config.mode == Mode.WARN:
            return Verdict(
                proceed=True,
                breached=limit,
                warning=f"guardrail '{limit.value}' exceeded ({value}/{cap})",
            )

        # GRACEFUL or HARD_STOP — localized exception, never a process kill.
        raise CostLimitExceededException(limit, value, cap, state.trace_id)
