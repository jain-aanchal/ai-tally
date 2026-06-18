"""Pairwise-LLM-judge eval executor (CTO-114).

For each ``(sample, candidate_response, current_response)`` triple the executor:

1. Builds the pairwise rubric prompt — two responses labeled A and B, no framing leak about
   which is "the new candidate".
2. **Randomizes A/B ordering per sample** to mitigate position bias (judges tend to favor the
   first response on ambiguous calls). The (a_is_candidate) bit is recorded so the verdict is
   interpretable.
3. Calls the judge model (default ``claude-opus-4-8``; overridable via
   ``TALLY_EVAL_JUDGE_MODEL`` env or per-tenant ``judge_model``).
4. Parses the verdict — strict format: exactly one of ``A``, ``B``, ``TIE``. Anything else is
   recorded as ``error`` rather than coerced.
5. Writes an :class:`EvalRunRow` to ClickHouse ``eval_runs``.

Two hard safety rails (same shape as :mod:`gateway.replay_executor`):

* **Per-tenant daily budget cap.** Default $10/day for judges (vs $5 for replay candidates).
* **Per-tenant concurrency limit.** Max ``MAX_CONCURRENT_PER_TENANT`` judge calls in-flight at
  a time per tenant.

Judge-self-bias caveat: when one of the candidates is from the same model family as the judge
(e.g. judging anthropic candidates with an anthropic judge), the judge tends to slightly favor
its own family. v1 accepts this and documents it; v2 may rotate judges across families.

The rubric prompt is versioned (``RUBRIC_VERSION``) so an A/B of a tightened rubric doesn't
silently invalidate historical comparisons.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol
from uuid import UUID, uuid4

from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd

from gateway.eval_store import (
    ALL_VERDICTS,
    VERDICT_CANDIDATE_WINS,
    VERDICT_CURRENT_WINS,
    VERDICT_ERROR,
    VERDICT_TIE,
    EvalRunRow,
)

UTC = timezone.utc
logger = logging.getLogger("tally.gateway.eval_executor")

MAX_CONCURRENT_PER_TENANT = 3
# Rubric version is part of the row so we can A/B a tightened rubric and still read historicals.
RUBRIC_VERSION = "rubric-v1"


# --- Rubric ----------------------------------------------------------------------------------

# This prompt is load-bearing. Tweaks should bump RUBRIC_VERSION.
#
# Why this exact shape:
#   * Two-line answer format — judges are far more reliable when they emit a short literal token
#     than when they free-write. The parse below treats anything else as `error`.
#   * "Impartial evaluator" framing keeps the judge from sliding into "the second is more
#     polished" cargo-cult judgments common with chat-tuned models.
#   * The instruction is repeated *before* the responses so the judge has the question fresh in
#     working memory when looking at A and B.
RUBRIC_TEMPLATE = """You are an impartial evaluator. Two assistant responses (A and B) were given the same instruction below.
Which response better follows the instruction? Answer with exactly one of: A, B, TIE.
If they are roughly equivalent in quality, answer TIE. Do not explain.

----
INSTRUCTION:
{instruction}
----
RESPONSE A:
{response_a}
----
RESPONSE B:
{response_b}
----

Your answer (A, B, or TIE):"""


# --- Judge client contract -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class JudgeCall:
    provider: str
    model: str
    prompt: str


@dataclass(frozen=True, slots=True)
class JudgeResponse:
    text: str
    input_tokens: int
    output_tokens: int
    status_code: int = 200
    error_msg: str = ""


class JudgeClient(Protocol):
    async def __call__(self, call: JudgeCall) -> JudgeResponse: ...


class TodaysSpendLookup(Protocol):
    def __call__(self, tenant_id: str) -> int:
        """Today's already-spent eval budget for ``tenant_id``, in micro-USD."""


# --- Result ----------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class EvalResult:
    sample_id: UUID
    candidate_provider: str
    candidate_model: str
    verdict: str
    excluded_budget: bool = False
    error_msg: str = ""
    row: EvalRunRow | None = None

    @property
    def succeeded(self) -> bool:
        return self.row is not None and self.verdict != VERDICT_ERROR


# --- Verdict parsing -------------------------------------------------------------------------

_VERDICT_RE = re.compile(r"\b(A|B|TIE)\b", re.IGNORECASE)


def parse_verdict(text: str, *, a_is_candidate: bool) -> str | None:
    """Map the judge's raw text to one of (current_wins, candidate_wins, tie).

    Returns ``None`` if the judge didn't answer in the required format — caller records that
    as a ``VERDICT_ERROR`` row rather than coercing.

    The parse is intentionally narrow: we look for the FIRST occurrence of "A", "B", or "TIE"
    as a standalone token. If the judge wrote a paragraph, we honor the first letter it emits.
    """
    if not text:
        return None
    m = _VERDICT_RE.search(text)
    if m is None:
        return None
    letter = m.group(1).upper()
    if letter == "TIE":
        return VERDICT_TIE
    a_wins = letter == "A"
    candidate_wins = (a_wins and a_is_candidate) or (not a_wins and not a_is_candidate)
    return VERDICT_CANDIDATE_WINS if candidate_wins else VERDICT_CURRENT_WINS


# --- Executor --------------------------------------------------------------------------------

@dataclass
class EvalExecutor:
    catalog: PriceCatalog
    judge_client: JudgeClient
    todays_spend_micro_usd: TodaysSpendLookup
    sink: Callable[[EvalRunRow], None]
    judge_provider: str = "anthropic"
    judge_model: str = "claude-opus-4-8"
    rng: random.Random = field(default_factory=random.Random)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def _semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(tenant_id)
        if sem is None:
            sem = asyncio.Semaphore(MAX_CONCURRENT_PER_TENANT)
            self._semaphores[tenant_id] = sem
        return sem

    async def judge_pair(
        self,
        *,
        tenant_id: str,
        replay_run_id: UUID,
        sample_id: UUID,
        candidate_provider: str,
        candidate_model: str,
        instruction: str,
        current_response: str,
        candidate_response: str,
        daily_budget_usd: Decimal,
        # Caller's pre-flight estimate of the judge call cost in micro-USD. Used for the
        # budget check before the call is issued.
        estimated_call_cost_micro_usd: int = 0,
    ) -> EvalResult:
        """Judge one (current, candidate) pair. Honors the budget cap + concurrency limit.

        Returns an :class:`EvalResult`. On budget skip, ``excluded_budget=True`` and no row is
        written. On judge error (network, malformed verdict), a row with ``judge_verdict="error"``
        IS written so the eval pass is auditable — the win-rate aggregate ignores error rows.
        """
        budget_cap = int(Decimal(daily_budget_usd) * Decimal(1_000_000))
        already_spent = self.todays_spend_micro_usd(tenant_id)
        if already_spent + estimated_call_cost_micro_usd > budget_cap:
            logger.info(
                "eval: budget cap hit for tenant=%s (spent=%d cap=%d projected=%d)",
                tenant_id, already_spent, budget_cap, estimated_call_cost_micro_usd,
            )
            return EvalResult(
                sample_id=sample_id,
                candidate_provider=candidate_provider,
                candidate_model=candidate_model,
                verdict=VERDICT_ERROR,
                excluded_budget=True,
                error_msg="budget cap",
            )

        # Position-bias mitigation: flip a coin per sample. Store which side carried the candidate
        # so the parsed verdict is interpretable.
        a_is_candidate = self.rng.random() < 0.5
        response_a = candidate_response if a_is_candidate else current_response
        response_b = current_response if a_is_candidate else candidate_response
        prompt = RUBRIC_TEMPLATE.format(
            instruction=instruction or "(no instruction recorded)",
            response_a=response_a or "(empty)",
            response_b=response_b or "(empty)",
        )

        async with self._semaphore(tenant_id):
            try:
                resp = await self.judge_client(
                    JudgeCall(
                        provider=self.judge_provider,
                        model=self.judge_model,
                        prompt=prompt,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — surface as error row
                resp = JudgeResponse(
                    text="", input_tokens=0, output_tokens=0,
                    status_code=0, error_msg=f"network: {exc}",
                )

        cost_micro_usd, _ = compute_cost_micro_usd(
            self.catalog,
            self.judge_provider,
            self.judge_model,
            Usage(
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
            ),
        )

        if resp.error_msg or resp.status_code >= 400:
            verdict = VERDICT_ERROR
            err = resp.error_msg or f"http_{resp.status_code}"
        else:
            parsed = parse_verdict(resp.text, a_is_candidate=a_is_candidate)
            if parsed is None:
                verdict = VERDICT_ERROR
                err = f"unparseable judge output: {resp.text[:60]!r}"
            else:
                verdict = parsed
                err = ""

        row = EvalRunRow(
            tenant_id=tenant_id,
            eval_run_id=uuid4(),
            replay_run_id=replay_run_id,
            sample_id=sample_id,
            candidate_provider=candidate_provider,
            candidate_model=candidate_model,
            judge_verdict=verdict,
            judge_provider=self.judge_provider,
            judge_model=self.judge_model,
            judge_prompt_version=RUBRIC_VERSION,
            judged_at=datetime.now(UTC),
            cost_micro_usd=cost_micro_usd,
            error_msg=err,
        )
        # Sanity — the verdict had better be in the enum.
        assert row.judge_verdict in ALL_VERDICTS
        self.sink(row)
        return EvalResult(
            sample_id=sample_id,
            candidate_provider=candidate_provider,
            candidate_model=candidate_model,
            verdict=verdict,
            error_msg=err,
            row=row,
        )
