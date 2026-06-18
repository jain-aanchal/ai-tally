-- replay_samples — opt-in 5% sample of real spans, kept for cross-provider replay (CTO-113).
--
-- Workflows 2 (Compare) and 5 (Estimate) need to project real candidate-model cost/latency from
-- the tenant's actual traffic. The mock projection is rescaled-fictional; this table is the input
-- to the real one. Each row points at a MinIO/S3 blob holding the resolved prompt + tool defs +
-- response so the replay executor can re-issue the call against a candidate model.
--
-- Per-tenant opt-in — see Postgres `tenant_replay_config`. Default OFF; no surprises.
--
-- PII scrubbing happens *before* the object is written. The PIIScrubbed flag exists so we can
-- prove (and audit) that a sample was scrubbed before storage; rows with PIIScrubbed=0 must never
-- be replayed.
--
-- ContextFidelity is the v1 honesty knob — we only replay resolved context (no live RAG / tool
-- execution). Future tiers ("live retrieval", "live tool execution") will widen the enum.

CREATE TABLE IF NOT EXISTS replay_samples
(
    TenantId         LowCardinality(String),
    SampleId         UUID,
    TraceId          String                  CODEC(ZSTD(1)),
    FeatureTag       LowCardinality(String),
    RealProvider     LowCardinality(String),
    RealModel        LowCardinality(String),
    InputTokens      UInt32                  CODEC(T64, ZSTD(1)),
    OutputTokens    UInt32                   CODEC(T64, ZSTD(1)),
    CapturedAt       DateTime64(9)           CODEC(Delta, ZSTD(1)),
    S3ObjectKey      String                  CODEC(ZSTD(1)),
    PIIScrubbed      UInt8,
    ContextFidelity  Enum8('resolved-context' = 1, 'live-retrieval' = 2) DEFAULT 'resolved-context'
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(CapturedAt)
ORDER BY (TenantId, SampleId);


-- replay_runs — outcomes from replaying a sample against a candidate model (CTO-113).
--
-- One row per (sample, candidate) attempt. CostMicroUsd uses the authoritative SDK price catalog
-- (CTO-106 expanded coverage) so projected savings are grounded.
CREATE TABLE IF NOT EXISTS replay_runs
(
    TenantId         LowCardinality(String),
    RunId            UUID,
    SampleId         UUID,
    CandidateProvider LowCardinality(String),
    CandidateModel   LowCardinality(String),
    InputTokens      UInt32                  CODEC(T64, ZSTD(1)),
    OutputTokens     UInt32                  CODEC(T64, ZSTD(1)),
    CostMicroUsd     UInt64                  CODEC(T64, ZSTD(1)),
    LatencyMs        UInt32                  CODEC(T64, ZSTD(1)),
    ErrorMsg         String                  CODEC(ZSTD(1)),
    RanAt            DateTime64(9)           CODEC(Delta, ZSTD(1)),
    ContextFidelity  Enum8('resolved-context' = 1, 'live-retrieval' = 2) DEFAULT 'resolved-context'
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(RanAt)
ORDER BY (TenantId, RunId);
