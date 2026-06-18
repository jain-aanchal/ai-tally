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

import json
import logging
import threading
import urllib.error
import urllib.request
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


# --------------------------------------------------------------------------------------------
# Control-plane refresh (CTO-116) — pull the active rule set from the gateway periodically.
#
# The gateway is the canonical source of truth. We poll on CONFIG_REFRESH_SECONDS (mirroring
# web/lib/guardrails.ts) and fail soft: if the gateway is unreachable, we keep enforcing the
# last-known config. A rule that hasn't loaded yet is a no-op — never a hard fail.
#
# Each rule emits two span attributes when it fires:
#   gen_ai.guardrail.{rule_id}.verdict in {"enforced","shadow_observed","passed"}
#   gen_ai.guardrail.{rule_id}.kind    in pii_gate | cost_cap | loop_limit | model_deprecation
# Shadow-state rules emit "shadow_observed" — they record what would have fired without altering
# the call. The dashboard counts shadow_observed/wk as the graduation signal.
# --------------------------------------------------------------------------------------------

CONFIG_REFRESH_SECONDS = 60

_logger = logging.getLogger("tally.guardrails")


class RuleKind(str, Enum):
    PII_GATE = "pii_gate"
    COST_CAP = "cost_cap"
    LOOP_LIMIT = "loop_limit"
    MODEL_DEPRECATION = "model_deprecation"


class RuleState(str, Enum):
    ENABLED = "enabled"
    SHADOW = "shadow"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ControlPlaneRule:
    rule_id: str
    kind: RuleKind
    state: RuleState
    params: dict


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """Per-rule outcome for span-attr emission.

    verdict is one of:
      - "enforced"        rule fired AND state=enabled, behavior altered
      - "shadow_observed" rule fired AND state=shadow, behavior NOT altered
      - "passed"          rule evaluated but did not fire
    """

    rule_id: str
    kind: RuleKind
    verdict: str

    def attrs(self) -> dict[str, str]:
        return {
            f"gen_ai.guardrail.{self.rule_id}.verdict": self.verdict,
            f"gen_ai.guardrail.{self.rule_id}.kind": self.kind.value,
        }


@dataclass(slots=True)
class GuardrailEngine:
    """Evaluates guardrails against per-trace state. Holds the OBSERVE-mode would-fire tally.

    For CTO-116, also pulls a tenant-scoped rule set from the gateway on a refresh interval and
    exposes :meth:`apply_rules` which evaluates the cached rules against a per-call context.
    """

    would_fire_counts: dict[Limit, int] = field(default_factory=dict)
    rules: list[ControlPlaneRule] = field(default_factory=list)
    shadow_fire_counts: dict[str, int] = field(default_factory=dict)
    _last_refresh_at: float = 0.0
    _refresh_seconds: int = CONFIG_REFRESH_SECONDS
    _gateway_url: str | None = None
    _tenant_id: str | None = None
    _stop: threading.Event | None = None
    _thread: threading.Thread | None = None

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
                warning=f"guardrail \'{limit.value}\' exceeded ({value}/{cap})",
            )

        # GRACEFUL or HARD_STOP — localized exception, never a process kill.
        raise CostLimitExceededException(limit, value, cap, state.trace_id)

    # ---- Control-plane refresh (CTO-116) -----------------------------------------------------

    @classmethod
    def from_gateway(
        cls,
        gateway_url: str,
        tenant_id: str,
        *,
        refresh_seconds: int = CONFIG_REFRESH_SECONDS,
        start: bool = True,
    ) -> GuardrailEngine:
        """Build an engine bound to a gateway tenant, sync rules once, optionally start the loop.

        Fail-soft: an unreachable gateway is logged but does not raise — the engine is returned
        with an empty rule list and the next refresh will retry.
        """
        engine = cls()
        engine._gateway_url = gateway_url.rstrip("/")
        engine._tenant_id = tenant_id
        engine._refresh_seconds = refresh_seconds
        engine._refresh_once()
        if start:
            engine.start_refresh()
        return engine

    def _refresh_once(self) -> None:
        """One sync attempt. Replaces ``self.rules`` on success; on error, keeps current rules."""
        import time as _time

        if not self._gateway_url or not self._tenant_id:
            return
        url = f"{self._gateway_url}/v1/tenant/guardrails"
        req = urllib.request.Request(url, headers={"x-tenant-id": self._tenant_id})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            raw_rules = payload.get("rules") or []
            parsed: list[ControlPlaneRule] = []
            for r in raw_rules:
                try:
                    parsed.append(
                        ControlPlaneRule(
                            rule_id=str(r["rule_id"]),
                            kind=RuleKind(r["kind"]),
                            state=RuleState(r["state"]),
                            params=dict(r.get("params") or {}),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    _logger.warning("guardrails: skipping malformed rule: %s", exc)
            self.rules = parsed
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            _logger.warning(
                "guardrails: refresh failed, keeping last-known rules (%d): %s",
                len(self.rules),
                exc,
            )
        finally:
            self._last_refresh_at = _time.time()

    def _run_refresh_loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            if self._stop.wait(self._refresh_seconds):
                return
            self._refresh_once()

    def start_refresh(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run_refresh_loop, name="guardrail-refresh", daemon=True
        )
        self._thread.start()

    def stop_refresh(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._stop = None

    def apply_rules(self, call_context: dict) -> list[RuleVerdict]:
        """Evaluate each loaded rule against ``call_context``.

        Returns one :class:`RuleVerdict` per non-disabled rule. The verdict string is:
          - "enforced"        rule fired and state=enabled (caller should alter behavior)
          - "shadow_observed" rule fired and state=shadow  (record only)
          - "passed"          rule did not fire

        Shadow firings increment ``shadow_fire_counts[rule_id]`` so the dashboard can count
        would-have-fired/wk before flipping to enabled.
        """
        out: list[RuleVerdict] = []
        for rule in self.rules:
            if rule.state == RuleState.DISABLED:
                continue
            fired = _rule_fires(rule, call_context)
            if fired:
                if rule.state == RuleState.ENABLED:
                    verdict = "enforced"
                else:
                    verdict = "shadow_observed"
                    self.shadow_fire_counts[rule.rule_id] = (
                        self.shadow_fire_counts.get(rule.rule_id, 0) + 1
                    )
            else:
                verdict = "passed"
            out.append(RuleVerdict(rule_id=rule.rule_id, kind=rule.kind, verdict=verdict))
        return out


def _rule_fires(rule: ControlPlaneRule, ctx: dict) -> bool:
    """Heuristic per-kind firing predicate. Keep these dumb on purpose — the gateway is the
    source of truth for what each kind means, the SDK just evaluates the cached predicate."""
    if rule.kind == RuleKind.PII_GATE:
        return bool(ctx.get("contains_pii"))
    if rule.kind == RuleKind.COST_CAP:
        cap = rule.params.get("max_cost_micro_usd")
        if cap is None:
            return False
        return float(ctx.get("cost_micro_usd", 0)) > float(cap)
    if rule.kind == RuleKind.LOOP_LIMIT:
        cap = rule.params.get("max_steps")
        if cap is None:
            return False
        return int(ctx.get("step_count", 0)) > int(cap)
    if rule.kind == RuleKind.MODEL_DEPRECATION:
        deprecated = rule.params.get("deprecated_models") or []
        return ctx.get("model") in deprecated
    return False
