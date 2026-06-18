-- eval_runs — pairwise LLM-judge verdicts on replayed candidate responses (CTO-114).
--
-- For each (sample, candidate) the replay executor produces a candidate response. The eval
-- harness pairs that candidate response with the *current* model's response (already on the
-- captured sample's envelope) and asks an impartial judge model which is better. Three verdicts
-- + an error sentinel:
--
--   * 'current_wins'   — judge picked the current-model response
--   * 'candidate_wins' — judge picked the candidate-model response
--   * 'tie'            — judge said roughly equivalent
--   * 'error'          — judge call failed (network / parse / budget cap)
--
-- Aggregate win-rate (candidate_wins / non-error) with a Wilson 95% interval is what /compare
-- surfaces as Quality. The mock `qualityScore` it replaces was a fabrication; this column is
-- grounded in a real pairwise LLM-judge.
--
-- Bias notes baked into the writer (see eval_executor.py):
--   * A/B order is randomized per sample to mitigate position bias; the judge never sees the
--     "this is candidate" vs "this is current" framing.
--   * Default judge is claude-opus-4-8 (highest capability). If a candidate is also a
--     claude-family model the judge-self-bias risk is non-zero; v1 accepts the trade-off and
--     documents it. v2 may rotate judges.
--
-- JudgePromptVersion lets us A/B the rubric over time without invalidating historical rows.

CREATE TABLE IF NOT EXISTS eval_runs
(
    TenantId          LowCardinality(String),
    EvalRunId         UUID,
    ReplayRunId       UUID,
    SampleId          UUID,
    CandidateProvider LowCardinality(String),
    CandidateModel    LowCardinality(String),
    JudgeVerdict      Enum8('current_wins' = 1, 'candidate_wins' = 2, 'tie' = 3, 'error' = 4),
    JudgeProvider     LowCardinality(String),
    JudgeModel        LowCardinality(String),
    JudgePromptVersion LowCardinality(String),
    JudgedAt          DateTime64(9)         CODEC(Delta, ZSTD(1)),
    CostMicroUsd      UInt64                CODEC(T64, ZSTD(1)),
    ErrorMsg          String                CODEC(ZSTD(1))
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(JudgedAt)
ORDER BY (TenantId, EvalRunId);
