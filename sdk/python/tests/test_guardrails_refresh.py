"""SDK guardrails control-plane refresh (CTO-116).

Covers the engine's gateway-poll fail-soft behavior and the shadow/enforce span-attr emission.
Uses a monkeypatched ``urllib.request.urlopen`` — no real network.
"""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

from tally.guardrails import (
    CONFIG_REFRESH_SECONDS,
    ControlPlaneRule,
    GuardrailEngine,
    RuleKind,
    RuleState,
)


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._buf = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._buf

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _ok_response(rules: list[dict]):
    def fake(req, timeout=None):  # noqa: ARG001
        return _FakeResp({"tenant_id": "t-acme", "rules": rules})

    return fake


def test_config_refresh_seconds_mirrors_web() -> None:
    # The web side advertises a 60s window — keep them in lockstep.
    assert CONFIG_REFRESH_SECONDS == 60


def test_initial_refresh_loads_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [
        {
            "rule_id": "gr_cost",
            "kind": "cost_cap",
            "state": "enabled",
            "params": {"max_cost_micro_usd": 1_000_000},
        },
        {
            "rule_id": "gr_pii",
            "kind": "pii_gate",
            "state": "shadow",
            "params": {},
        },
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway(
        "http://gw.local", "t-acme", start=False
    )
    assert len(engine.rules) == 2
    assert engine.rules[0].kind == RuleKind.COST_CAP
    assert engine.rules[1].state == RuleState.SHADOW


def test_fail_soft_on_gateway_unreachable_keeps_last_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = [
        {
            "rule_id": "gr_loop",
            "kind": "loop_limit",
            "state": "shadow",
            "params": {"max_steps": 50},
        }
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway(
        "http://gw.local", "t-acme", start=False
    )
    assert len(engine.rules) == 1
    # Next refresh blows up — engine should keep the cached rule set.
    def boom(req, timeout=None):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("tally.guardrails.urllib.request.urlopen", boom)
    engine._refresh_once()
    assert len(engine.rules) == 1
    assert engine.rules[0].rule_id == "gr_loop"


def test_enabled_rule_emits_enforced_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [
        {
            "rule_id": "gr_cost",
            "kind": "cost_cap",
            "state": "enabled",
            "params": {"max_cost_micro_usd": 1000},
        }
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway("http://gw.local", "t-acme", start=False)
    verdicts = engine.apply_rules({"cost_micro_usd": 5000})
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.verdict == "enforced"
    attrs = v.attrs()
    assert attrs["gen_ai.guardrail.gr_cost.verdict"] == "enforced"
    assert attrs["gen_ai.guardrail.gr_cost.kind"] == "cost_cap"


def test_shadow_rule_emits_shadow_observed_and_does_not_alter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = [
        {
            "rule_id": "gr_cost",
            "kind": "cost_cap",
            "state": "shadow",
            "params": {"max_cost_micro_usd": 1000},
        }
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway("http://gw.local", "t-acme", start=False)
    verdicts = engine.apply_rules({"cost_micro_usd": 5000})
    assert verdicts[0].verdict == "shadow_observed"
    assert engine.shadow_fire_counts["gr_cost"] == 1
    # Firing again increments the counter — apply_rules itself never raises.
    engine.apply_rules({"cost_micro_usd": 5000})
    assert engine.shadow_fire_counts["gr_cost"] == 2


def test_passed_when_not_fired(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [
        {
            "rule_id": "gr_cost",
            "kind": "cost_cap",
            "state": "enabled",
            "params": {"max_cost_micro_usd": 1_000_000},
        }
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway("http://gw.local", "t-acme", start=False)
    verdicts = engine.apply_rules({"cost_micro_usd": 100})
    assert verdicts[0].verdict == "passed"
    assert "gr_cost" not in engine.shadow_fire_counts


def test_disabled_rule_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [
        {
            "rule_id": "gr_off",
            "kind": "cost_cap",
            "state": "disabled",
            "params": {"max_cost_micro_usd": 1},
        }
    ]
    monkeypatch.setattr(
        "tally.guardrails.urllib.request.urlopen", _ok_response(rules)
    )
    engine = GuardrailEngine.from_gateway("http://gw.local", "t-acme", start=False)
    verdicts = engine.apply_rules({"cost_micro_usd": 999_999})
    assert verdicts == []


def test_refresh_loop_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        return _FakeResp({"tenant_id": "t-acme", "rules": []})

    monkeypatch.setattr("tally.guardrails.urllib.request.urlopen", fake)
    engine = GuardrailEngine.from_gateway(
        "http://gw.local", "t-acme", refresh_seconds=0.1, start=True  # type: ignore[arg-type]
    )
    try:
        time.sleep(0.35)
    finally:
        engine.stop_refresh()
    # 1 initial + at least 2 loop iterations within 0.35s.
    assert calls["n"] >= 3, f"expected >=3 refresh calls, got {calls['n']}"
