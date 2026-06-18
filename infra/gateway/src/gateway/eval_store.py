"""ClickHouse row shape + sink helpers for the eval harness (CTO-114).

Mirrors :mod:`gateway.replay_store` for the new ``eval_runs`` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


EVAL_RUN_COLS = (
    "TenantId", "EvalRunId", "ReplayRunId", "SampleId",
    "CandidateProvider", "CandidateModel",
    "JudgeVerdict",
    "JudgeProvider", "JudgeModel", "JudgePromptVersion",
    "JudgedAt", "CostMicroUsd", "ErrorMsg",
)

# Verdict strings — match the ClickHouse Enum8 ordering in db/clickhouse/eval_runs.sql.
VERDICT_CURRENT_WINS = "current_wins"
VERDICT_CANDIDATE_WINS = "candidate_wins"
VERDICT_TIE = "tie"
VERDICT_ERROR = "error"
ALL_VERDICTS = frozenset((
    VERDICT_CURRENT_WINS, VERDICT_CANDIDATE_WINS, VERDICT_TIE, VERDICT_ERROR,
))


@dataclass(frozen=True, slots=True)
class EvalRunRow:
    tenant_id: str
    eval_run_id: UUID
    replay_run_id: UUID
    sample_id: UUID
    candidate_provider: str
    candidate_model: str
    judge_verdict: str  # one of ALL_VERDICTS
    judge_provider: str
    judge_model: str
    judge_prompt_version: str
    judged_at: datetime
    cost_micro_usd: int
    error_msg: str = ""

    def as_clickhouse_row(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            str(self.eval_run_id),
            str(self.replay_run_id),
            str(self.sample_id),
            self.candidate_provider,
            self.candidate_model,
            self.judge_verdict,
            self.judge_provider,
            self.judge_model,
            self.judge_prompt_version,
            self.judged_at,
            self.cost_micro_usd,
            self.error_msg,
        )
