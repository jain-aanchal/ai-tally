"""Tests for the pluggable eval framework (CTO-61)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tally.evals import (
    CorrectnessEvaluator,
    EvalHarness,
    EvalRegistry,
    EvalResult,
    EvalSummary,
    Evaluator,
    FormatAdherenceEvaluator,
    FormatKind,
    JudgeVerdict,
    RefusalEvaluator,
    Sample,
    default_registry,
)

# --- Interface / registry -----------------------------------------------------------------------


def test_default_evals_satisfy_protocol():
    judge = lambda p, o, r: 1.0  # noqa: E731
    for ev in (CorrectnessEvaluator(judge=judge), FormatAdherenceEvaluator(), RefusalEvaluator()):
        assert isinstance(ev, Evaluator)


def test_registry_register_and_lookup_custom_evaluator():
    class AlwaysOne:
        name = "always_one"

        def evaluate(self, sample: Sample) -> EvalResult:
            return EvalResult(self.name, sample.model, 1.0, True)

    reg = EvalRegistry()
    reg.register(AlwaysOne())
    assert reg.names() == ["always_one"]
    assert isinstance(reg.get("always_one"), Evaluator)


def test_registry_rejects_duplicate_and_nameless():
    reg = EvalRegistry()
    reg.register(RefusalEvaluator())
    with pytest.raises(ValueError):
        reg.register(RefusalEvaluator())

    class Nameless:
        name = ""

        def evaluate(self, sample):  # pragma: no cover - never reached
            ...

    with pytest.raises(ValueError):
        reg.register(Nameless())


# --- Correctness (LLM-as-judge, injected judge) -------------------------------------------------


def test_correctness_uses_injected_judge_score():
    judge = lambda p, o, r: 0.9  # noqa: E731
    ev = CorrectnessEvaluator(judge=judge)
    res = ev.evaluate(Sample(prompt="q", output="a", reference="a", model="m"))
    assert res.score == 0.9 and res.passed is True
    assert res.eval_cost_micro_usd == 0  # bare float => no reported judge cost


def test_correctness_clamps_out_of_range_judge_scores():
    ev = CorrectnessEvaluator(judge=lambda p, o, r: 5.0)
    assert ev.evaluate(Sample("q", "a")).score == 1.0
    ev2 = CorrectnessEvaluator(judge=lambda p, o, r: -3.0)
    assert ev2.evaluate(Sample("q", "a")).score == 0.0


def test_correctness_never_calls_judge_with_none_output():
    seen: list[str] = []

    def judge(p, o, r):
        seen.append(o)
        return 0.0

    CorrectnessEvaluator(judge=judge).evaluate(Sample("q", None))
    assert seen == [""]  # None coerced to "" before reaching the judge


def test_correctness_judge_exception_scores_zero():
    def boom(p, o, r):
        raise RuntimeError("network down")

    res = CorrectnessEvaluator(judge=boom).evaluate(Sample("q", "a"))
    assert res.score == 0.0 and res.passed is False
    assert "judge error" in res.detail


# --- Eval cost surfaced separately --------------------------------------------------------------


def test_judge_token_usage_priced_into_eval_cost():
    verdict = JudgeVerdict(score=1.0, judge_input_tokens=1_000_000, judge_output_tokens=0)
    ev = CorrectnessEvaluator(
        judge=lambda p, o, r: verdict,
        judge_input_micro_usd_per_token=Decimal("0.0000025"),
    )
    res = ev.evaluate(Sample("q", "a"))
    # 1e6 tokens * 0.0000025 micro-USD = 2.5 USD -> usd_to_micro = 2_500_000 micro-USD
    assert res.eval_cost_micro_usd == 2_500_000
    assert res.score == 1.0


def test_eval_cost_is_separate_from_model_cost_in_summary():
    verdict = JudgeVerdict(score=1.0, judge_output_tokens=100_000)
    reg = EvalRegistry()
    reg.register(CorrectnessEvaluator(judge=lambda p, o, r: verdict))
    summary = EvalHarness(reg).run([Sample("q", "a", model="m")])
    mq = summary.quality_for("m")
    assert mq is not None
    assert mq.eval_cost_micro_usd > 0
    assert summary.total_eval_cost_micro_usd == mq.eval_cost_micro_usd


# --- Format adherence ---------------------------------------------------------------------------


def test_format_json_pass_and_fail():
    ev = FormatAdherenceEvaluator(kind=FormatKind.JSON)
    assert ev.evaluate(Sample("q", '{"a": 1}')).passed is True
    bad = ev.evaluate(Sample("q", "not json"))
    assert bad.passed is False and bad.score == 0.0


def test_format_required_keys():
    ev = FormatAdherenceEvaluator(kind=FormatKind.REQUIRED_KEYS, required_keys=("name", "age"))
    assert ev.evaluate(Sample("q", '{"name": "x", "age": 3}')).passed is True
    miss = ev.evaluate(Sample("q", '{"name": "x"}'))
    assert miss.passed is False and "missing keys" in miss.detail


def test_format_regex():
    ev = FormatAdherenceEvaluator(kind=FormatKind.REGEX, pattern=r"^ID-\d+$")
    assert ev.evaluate(Sample("q", "ID-42")).passed is True
    assert ev.evaluate(Sample("q", "nope")).passed is False


def test_format_none_output_fails_cleanly():
    ev = FormatAdherenceEvaluator(kind=FormatKind.JSON)
    res = ev.evaluate(Sample("q", None))
    assert res.passed is False and res.score == 0.0


# --- Refusal ------------------------------------------------------------------------------------


def test_refusal_detects_boilerplate():
    ev = RefusalEvaluator()
    assert ev.evaluate(Sample("q", "I can't help with that request.")).passed is False
    assert ev.evaluate(Sample("q", "I'm   UNABLE to do this")).passed is False  # spacing/casing
    assert ev.evaluate(Sample("q", "Sure, here is the answer.")).passed is True


def test_refusal_empty_or_none_is_refused():
    ev = RefusalEvaluator()
    assert ev.evaluate(Sample("q", None)).passed is False
    assert ev.evaluate(Sample("q", "   ")).passed is False


def test_refusal_extra_patterns():
    ev = RefusalEvaluator(extra_patterns=("not permitted by policy",))
    assert ev.evaluate(Sample("q", "That is Not Permitted By Policy.")).passed is False


def test_refusal_rate_aggregated():
    reg = EvalRegistry()
    reg.register(RefusalEvaluator())
    samples = [
        Sample("q", "ok answer", model="m"),
        Sample("q", "I'm unable to", model="m"),
        Sample("q", "another fine answer", model="m"),
        Sample("q", None, model="m"),
    ]
    summary = EvalHarness(reg).run(samples)
    bd = summary.quality_for("m").breakdown[0]
    assert bd.evaluator == "refusal"
    assert bd.pass_rate == 0.5  # 2 of 4 refused -> 0.5 not refused


# --- Aggregation + delta ------------------------------------------------------------------------


def _scripted_judge(scores: dict[str, float]):
    return lambda p, o, r: scores.get(o, 0.0)


def test_per_model_score_and_delta_vs_baseline():
    scores = {"cheap-good": 0.6, "cheap-bad": 0.2, "premium": 0.9}
    reg = default_registry(judge=_scripted_judge(scores))
    samples = [
        Sample("q", "premium", model="premium", reference="r"),
        Sample("q", "cheap-bad", model="candidate", reference="r"),
    ]
    summary = EvalHarness(reg).run(samples, baseline="premium")
    assert isinstance(summary, EvalSummary)
    assert summary.baseline == "premium"
    assert len(summary.deltas) == 1
    delta = summary.deltas[0]
    assert delta.model == "candidate" and delta.baseline == "premium"
    # candidate scored worse on correctness -> negative overall delta
    assert delta.overall_delta < 0
    assert delta.per_eval_delta["correctness"] < 0


def test_summary_as_dict_round_trips_structure():
    reg = default_registry(judge=lambda p, o, r: 1.0)
    summary = EvalHarness(reg).run([Sample("q", '{"k": 1}', model="m")], baseline="m")
    d = summary.as_dict()
    assert d["baseline"] == "m"
    assert d["models"][0]["model"] == "m"
    assert "breakdown" in d["models"][0]
    assert d["deltas"] == []  # baseline has no delta vs. itself


def test_overall_score_equal_weights_evaluators():
    # JSON-valid + non-refusal + correctness 1.0 over a single model => overall 1.0
    reg = default_registry(judge=lambda p, o, r: 1.0)
    summary = EvalHarness(reg).run([Sample("q", '{"ok": true}', model="m")])
    assert summary.quality_for("m").overall_score == pytest.approx(1.0)


def test_unknown_baseline_yields_no_deltas():
    reg = default_registry(judge=lambda p, o, r: 1.0)
    summary = EvalHarness(reg).run([Sample("q", "a", model="m")], baseline="does-not-exist")
    assert summary.deltas == ()


# --- Defensiveness ------------------------------------------------------------------------------


def test_broken_evaluator_does_not_crash_run():
    class Broken:
        name = "broken"

        def evaluate(self, sample):
            raise ValueError("boom")

    reg = EvalRegistry()
    reg.register(Broken())
    summary = EvalHarness(reg).run([Sample("q", "a", model="m")])
    bd = summary.quality_for("m").breakdown[0]
    assert bd.evaluator == "broken" and bd.mean_score == 0.0


def test_sample_output_text_coerces_non_string():
    assert Sample("q", None).output_text() == ""
    assert Sample("q", 123).output_text() == "123"  # type: ignore[arg-type]
