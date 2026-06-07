# SPDX-License-Identifier: Apache-2.0
"""Pluggable eval framework — attach a quality signal to cost comparisons.

Implements CTO-61 (spec §9 W1).

When comparing models/prompts on cost alone, a cheaper-but-worse model looks like a win. These
evals attach a *quality* signal so a model swap shows a quality delta alongside the cost delta.
Evals run over **replayed outputs** (the replay engine is CTO-59, out of scope here): you receive
already-produced outputs as plain :class:`Sample` data and score them.

This module is pure logic — **no network, no real LLM calls**. The "LLM-as-judge" correctness eval
takes an **injected judge callable** the caller supplies, so tests are deterministic and offline.
The judge reports its own token usage, which we accumulate into ``eval_cost_micro_usd`` — surfaced
**separately** from the inference cost being evaluated (a cheaper model must not look better just
because judging it was cheap).

Design:

- :class:`Evaluator` — a stable protocol: a ``name`` and ``evaluate(sample) -> EvalResult``.
  Stateless and composable. Users register custom evaluators by name on a :class:`EvalRegistry`.
- Three defaults ship: :class:`CorrectnessEvaluator` (LLM-as-judge, injected judge),
  :class:`FormatAdherenceEvaluator` (JSON / regex / required-keys), :class:`RefusalEvaluator`.
- :class:`EvalHarness` holds the registry, runs evaluators over samples, aggregates per-model
  scores, and computes a delta vs. a designated baseline ("current") model.

Defensiveness: a ``None`` / garbage output must never raise — it scores as failing (or refused, as
appropriate). Money mirrors :mod:`tally.pricing`: integer micro-USD, :class:`~decimal.Decimal` for
rate math, never float dollars.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from tally.schema import usd_to_micro

# --- Sample (the unit of evaluation) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sample:
    """One replayed output to be scored.

    A frozen dataclass so it is hashable and never mutated in place. ``model`` identifies which
    model produced ``output`` (used for per-model aggregation). ``reference`` is the
    expected/gold answer or criteria string for judge-style evals; ``metadata`` carries optional
    tool-call info or anything an evaluator wants (open dict).
    """

    prompt: str
    output: str | None
    model: str = "unknown"
    reference: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def output_text(self) -> str:
        """The output coerced to a safe string. ``None``/non-str become ``""`` — never raises."""
        if isinstance(self.output, str):
            return self.output
        if self.output is None:
            return ""
        return str(self.output)


# --- Eval result --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalResult:
    """A single (evaluator, sample) outcome.

    ``score`` is in ``[0, 1]`` (1 == best). ``passed`` is a boolean view for pass/fail evals.
    ``eval_cost_micro_usd`` is the cost the evaluator itself incurred (non-zero only for
    judge-style evals) — kept distinct from the inference cost of the model under test.
    """

    evaluator: str
    model: str
    score: float
    passed: bool
    eval_cost_micro_usd: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "model": self.model,
            "score": self.score,
            "passed": self.passed,
            "eval_cost_micro_usd": self.eval_cost_micro_usd,
            "detail": self.detail,
        }


def _clamp01(value: float) -> float:
    """Clamp to ``[0, 1]``; coerce non-finite/garbage to 0.0 — never raises."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


# --- Evaluator protocol + registry --------------------------------------------------------------


@runtime_checkable
class Evaluator(Protocol):
    """The pluggable eval interface. Implement ``name`` and ``evaluate``; keep it stateless.

    Custom evaluators only need to satisfy this protocol and be registered by name on an
    :class:`EvalRegistry`. Dependency injection (e.g. the judge callable) is passed to the
    evaluator's constructor by the caller, never reached for globally.
    """

    name: str

    def evaluate(self, sample: Sample) -> EvalResult: ...


class EvalRegistry:
    """Name → :class:`Evaluator` registry. Lets users register custom evaluators."""

    def __init__(self) -> None:
        self._evaluators: dict[str, Evaluator] = {}

    def register(self, evaluator: Evaluator) -> Evaluator:
        """Register ``evaluator`` under its ``name``. Raises on duplicate names."""
        name = getattr(evaluator, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError("evaluator must expose a non-empty string 'name'")
        if name in self._evaluators:
            raise ValueError(f"evaluator '{name}' already registered")
        self._evaluators[name] = evaluator
        return evaluator

    def get(self, name: str) -> Evaluator:
        return self._evaluators[name]

    def names(self) -> list[str]:
        return list(self._evaluators)

    def all(self) -> list[Evaluator]:
        return list(self._evaluators.values())


# --- Default 1: correctness (LLM-as-judge, injected judge) ---------------------------------------


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """What an injected judge returns.

    ``score`` in ``[0, 1]``. The judge reports the tokens *it* consumed so we can price the eval
    separately from the model under test. A judge that does not track tokens leaves them at 0.
    """

    score: float
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    rationale: str = ""


@runtime_checkable
class Judge(Protocol):
    """Injected judge callable. The caller supplies this — we never call a network ourselves.

    A judge may return a :class:`JudgeVerdict` or a bare float in ``[0, 1]`` (then it incurs no
    reported eval cost).
    """

    def __call__(
        self, prompt: str, output: str, reference: str | None
    ) -> JudgeVerdict | float: ...


@dataclass(frozen=True, slots=True)
class CorrectnessEvaluator:
    """LLM-as-judge correctness — scores ``output`` vs. ``reference``/criteria.

    The ``judge`` is **injected**; this evaluator never touches the network. The judge's token
    usage is priced at ``judge_*_micro_usd_per_token`` (Decimal rate math, integer micro-USD out)
    and surfaced as ``eval_cost_micro_usd``, kept distinct from the model's inference cost.

    Defensive: a ``None``/garbage output, a missing reference, or a judge that raises all yield a
    failing (score 0.0) result rather than propagating.
    """

    judge: Judge
    name: str = "correctness"
    pass_threshold: float = 0.5
    #: per-token judge rates (micro-USD). Default values are illustrative placeholders.
    judge_input_micro_usd_per_token: Decimal = Decimal("0.0000025")
    judge_output_micro_usd_per_token: Decimal = Decimal("0.00001")

    def _judge_cost(self, in_tok: int, out_tok: int) -> int:
        in_tok = max(0, int(in_tok or 0))
        out_tok = max(0, int(out_tok or 0))
        total = self.judge_input_micro_usd_per_token * Decimal(in_tok) + (
            self.judge_output_micro_usd_per_token * Decimal(out_tok)
        )
        return usd_to_micro(total)

    def evaluate(self, sample: Sample) -> EvalResult:
        output = sample.output_text()
        try:
            verdict = self.judge(sample.prompt, output, sample.reference)
        except Exception as exc:  # noqa: BLE001 — never let a judge crash the harness
            return EvalResult(
                evaluator=self.name,
                model=sample.model,
                score=0.0,
                passed=False,
                eval_cost_micro_usd=0,
                detail=f"judge error: {type(exc).__name__}",
            )

        if isinstance(verdict, JudgeVerdict):
            score = _clamp01(verdict.score)
            cost = self._judge_cost(verdict.judge_input_tokens, verdict.judge_output_tokens)
            detail = verdict.rationale
        else:
            score = _clamp01(verdict)
            cost = 0
            detail = ""

        return EvalResult(
            evaluator=self.name,
            model=sample.model,
            score=score,
            passed=score >= self.pass_threshold,
            eval_cost_micro_usd=cost,
            detail=detail,
        )


# --- Default 2: format adherence ----------------------------------------------------------------


class FormatKind(str, Enum):
    JSON = "json"
    REGEX = "regex"
    REQUIRED_KEYS = "required_keys"


@dataclass(frozen=True, slots=True)
class FormatAdherenceEvaluator:
    """Validates the output conforms to an expected structure. Pure and deterministic.

    Supports three checks (choose via ``kind``):

    - ``JSON``: the output must parse as JSON. If ``required_keys`` is set, the parsed value must be
      an object containing all of them.
    - ``REGEX``: the output must fully match ``pattern`` (``re.search`` semantics).
    - ``REQUIRED_KEYS``: parse as JSON, then require all ``required_keys`` (shorthand for JSON +
      keys).

    Score is binary (1.0 pass / 0.0 fail). Malformed/``None`` output fails cleanly.
    """

    name: str = "format_adherence"
    kind: FormatKind = FormatKind.JSON
    pattern: str | None = None
    required_keys: tuple[str, ...] = ()

    def _check(self, text: str) -> tuple[bool, str]:
        if self.kind is FormatKind.REGEX:
            if not self.pattern:
                return False, "no regex pattern configured"
            try:
                ok = re.search(self.pattern, text) is not None
            except re.error as exc:
                return False, f"bad regex: {exc}"
            return ok, "" if ok else "regex did not match"

        # JSON or REQUIRED_KEYS both need a JSON parse.
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as exc:
            return False, f"not JSON-parseable: {type(exc).__name__}"

        keys = self.required_keys
        if not keys:
            return True, ""
        if not isinstance(parsed, dict):
            return False, "JSON is not an object; cannot check required keys"
        missing = [k for k in keys if k not in parsed]
        if missing:
            return False, f"missing keys: {sorted(missing)}"
        return True, ""

    def evaluate(self, sample: Sample) -> EvalResult:
        ok, detail = self._check(sample.output_text())
        return EvalResult(
            evaluator=self.name,
            model=sample.model,
            score=1.0 if ok else 0.0,
            passed=ok,
            eval_cost_micro_usd=0,
            detail=detail,
        )


# --- Default 3: refusal rate --------------------------------------------------------------------

#: Defensive refusal phrases (lowercased substrings). Boilerplate policy-style declines.
_REFUSAL_PATTERNS: tuple[str, ...] = (
    "i can't help with that",
    "i cannot help with that",
    "i can't assist with that",
    "i cannot assist with that",
    "i'm unable to",
    "i am unable to",
    "i'm not able to",
    "i am not able to",
    "i can't provide",
    "i cannot provide",
    "i won't be able to",
    "i will not be able to",
    "i'm sorry, but i can't",
    "i'm sorry, but i cannot",
    "as an ai",
    "i cannot comply",
    "i can't comply",
    "against my guidelines",
    "violates my guidelines",
    "i must decline",
    "i have to decline",
    "unable to assist with this request",
)


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace so spacing/casing variations still match."""
    return re.sub(r"\s+", " ", text.lower()).strip()


@dataclass(frozen=True, slots=True)
class RefusalEvaluator:
    """Detects model refusals via a defensive substring matcher.

    A matched refusal scores 0.0 (refused); a clean answer scores 1.0. The aggregate refusal *rate*
    is computed by the harness over a model's samples. ``None``/empty output is treated as a
    non-answer → refused. Extra patterns can be supplied for domain-specific boilerplate.
    """

    name: str = "refusal"
    extra_patterns: tuple[str, ...] = ()

    def _patterns(self) -> tuple[str, ...]:
        return _REFUSAL_PATTERNS + tuple(p.lower() for p in self.extra_patterns)

    def is_refusal(self, text: str | None) -> bool:
        norm = _normalize(text if isinstance(text, str) else "")
        if not norm:
            return True  # empty/None output is a non-answer
        return any(p in norm for p in self._patterns())

    def evaluate(self, sample: Sample) -> EvalResult:
        refused = self.is_refusal(sample.output)
        return EvalResult(
            evaluator=self.name,
            model=sample.model,
            score=0.0 if refused else 1.0,
            passed=not refused,
            eval_cost_micro_usd=0,
            detail="refused" if refused else "",
        )


# --- Aggregation: per-model quality score + delta vs. current ------------------------------------


@dataclass(frozen=True, slots=True)
class EvalBreakdown:
    """Per-(model, evaluator) aggregate over a set of samples."""

    evaluator: str
    sample_count: int
    mean_score: float
    pass_rate: float
    eval_cost_micro_usd: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "sample_count": self.sample_count,
            "mean_score": self.mean_score,
            "pass_rate": self.pass_rate,
            "eval_cost_micro_usd": self.eval_cost_micro_usd,
        }


@dataclass(frozen=True, slots=True)
class ModelQuality:
    """Per-model quality summary: overall score + per-eval breakdown + total eval cost."""

    model: str
    overall_score: float
    sample_count: int
    breakdown: tuple[EvalBreakdown, ...]
    #: total cost of *running the evals* on this model — distinct from its inference cost.
    eval_cost_micro_usd: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "overall_score": self.overall_score,
            "sample_count": self.sample_count,
            "eval_cost_micro_usd": self.eval_cost_micro_usd,
            "breakdown": [b.as_dict() for b in self.breakdown],
        }


@dataclass(frozen=True, slots=True)
class ModelDelta:
    """Quality delta of a candidate model vs. the baseline ("current") model."""

    model: str
    baseline: str
    overall_score: float
    baseline_score: float
    overall_delta: float
    #: per-evaluator score delta vs. baseline (candidate − baseline)
    per_eval_delta: Mapping[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "baseline": self.baseline,
            "overall_score": self.overall_score,
            "baseline_score": self.baseline_score,
            "overall_delta": self.overall_delta,
            "per_eval_delta": dict(self.per_eval_delta),
        }


@dataclass(frozen=True, slots=True)
class EvalSummary:
    """Immutable run summary: per-model quality, deltas vs. baseline, total eval cost."""

    baseline: str | None
    models: tuple[ModelQuality, ...]
    deltas: tuple[ModelDelta, ...]
    total_eval_cost_micro_usd: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "total_eval_cost_micro_usd": self.total_eval_cost_micro_usd,
            "models": [m.as_dict() for m in self.models],
            "deltas": [d.as_dict() for d in self.deltas],
        }

    def quality_for(self, model: str) -> ModelQuality | None:
        for m in self.models:
            if m.model == model:
                return m
        return None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class EvalHarness:
    """Holds the registry, runs evaluators over samples, aggregates, and computes deltas.

    Evaluators stay stateless; the harness owns iteration and aggregation. Eval cost is summed per
    model (and overall) entirely from :attr:`EvalResult.eval_cost_micro_usd`, so it is always
    distinct from the inference cost of the model under test.
    """

    def __init__(self, registry: EvalRegistry | None = None) -> None:
        self.registry = registry or EvalRegistry()

    def run(
        self, samples: Sequence[Sample], *, baseline: str | None = None
    ) -> EvalSummary:
        evaluators = self.registry.all()
        # results[model][evaluator] -> list[EvalResult]
        results: dict[str, dict[str, list[EvalResult]]] = {}
        for sample in samples:
            for ev in evaluators:
                try:
                    res = ev.evaluate(sample)
                except Exception:  # noqa: BLE001 — a broken evaluator scores 0, never crashes run
                    res = EvalResult(
                        evaluator=getattr(ev, "name", "unknown"),
                        model=sample.model,
                        score=0.0,
                        passed=False,
                    )
                results.setdefault(sample.model, {}).setdefault(res.evaluator, []).append(res)

        models = tuple(self._summarize_model(m, by_eval) for m, by_eval in results.items())
        models = tuple(sorted(models, key=lambda m: m.model))
        total_eval_cost = sum(m.eval_cost_micro_usd for m in models)
        deltas = self._compute_deltas(models, baseline)
        return EvalSummary(
            baseline=baseline,
            models=models,
            deltas=deltas,
            total_eval_cost_micro_usd=total_eval_cost,
        )

    @staticmethod
    def _summarize_model(
        model: str, by_eval: Mapping[str, list[EvalResult]]
    ) -> ModelQuality:
        breakdown: list[EvalBreakdown] = []
        per_eval_means: list[float] = []
        eval_cost = 0
        sample_count = 0
        for ev_name, res_list in sorted(by_eval.items()):
            scores = [r.score for r in res_list]
            passes = [1.0 if r.passed else 0.0 for r in res_list]
            ev_cost = sum(r.eval_cost_micro_usd for r in res_list)
            eval_cost += ev_cost
            sample_count = max(sample_count, len(res_list))
            mean_score = _mean(scores)
            per_eval_means.append(mean_score)
            breakdown.append(
                EvalBreakdown(
                    evaluator=ev_name,
                    sample_count=len(res_list),
                    mean_score=mean_score,
                    pass_rate=_mean(passes),
                    eval_cost_micro_usd=ev_cost,
                )
            )
        overall = _mean(per_eval_means)  # equal-weight across evaluators
        return ModelQuality(
            model=model,
            overall_score=overall,
            sample_count=sample_count,
            breakdown=tuple(breakdown),
            eval_cost_micro_usd=eval_cost,
        )

    @staticmethod
    def _compute_deltas(
        models: Sequence[ModelQuality], baseline: str | None
    ) -> tuple[ModelDelta, ...]:
        if baseline is None:
            return ()
        base = next((m for m in models if m.model == baseline), None)
        if base is None:
            return ()
        base_evals = {b.evaluator: b.mean_score for b in base.breakdown}
        deltas: list[ModelDelta] = []
        for m in models:
            if m.model == baseline:
                continue
            cand_evals = {b.evaluator: b.mean_score for b in m.breakdown}
            keys = set(base_evals) | set(cand_evals)
            per_eval_delta = {
                k: cand_evals.get(k, 0.0) - base_evals.get(k, 0.0) for k in sorted(keys)
            }
            deltas.append(
                ModelDelta(
                    model=m.model,
                    baseline=baseline,
                    overall_score=m.overall_score,
                    baseline_score=base.overall_score,
                    overall_delta=m.overall_score - base.overall_score,
                    per_eval_delta=per_eval_delta,
                )
            )
        return tuple(sorted(deltas, key=lambda d: d.model))


def default_registry(judge: Judge) -> EvalRegistry:
    """A registry preloaded with the three default evals.

    ``judge`` is the injected LLM-as-judge callable (offline/stub in tests). Format adherence
    defaults to a plain JSON-parseable check; refusal uses the built-in pattern set. Callers who
    want different format/refusal config should register those evaluators themselves.
    """
    registry = EvalRegistry()
    registry.register(CorrectnessEvaluator(judge=judge))
    registry.register(FormatAdherenceEvaluator())
    registry.register(RefusalEvaluator())
    return registry
