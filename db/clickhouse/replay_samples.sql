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
ORDER BY (TenantId, SampleId)
-- Retention (CTO-338): OPT-IN CORPUS, 30 days. This number is not a new product decision. It is
-- the default of `tenant_replay_config.retention_days` (db/postgres/0004_tenant_replay_config.sql),
-- which is described there as capping "how long the captured payloads live" and is disclosed to
-- the tenant verbatim in gateway.tenant_replay.CANDIDATE_RESPONSE_RETENTION_CONSENT when they opt
-- in. The blob side says the same (replay_store.py: lifecycle rules are out of band). ClickHouse
-- simply never enforced the promise, so this row index grew to 903k rows while the gateway only
-- ever reads the newest 5000 of them (store.recent_replay_samples, once, at boot). Enforcing a
-- horizon we already stated beats inventing a new one.
--
-- PER-TENANT: retention_days is per tenant, and ClickHouse TTL is table-level, so a deployment with
-- non-default values compiles them into a multiIf keyed on TenantId exactly as raw spans do. Render
-- it with tally.storage_tiering.render_tenant_delete_expression; see storage_tiering.sql.
TTL toDateTime(CapturedAt) + INTERVAL 30 DAY DELETE;


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
ORDER BY (TenantId, RunId)
-- Retention (CTO-338): OPT-IN CORPUS, 30 days, and here it is not merely a promise kept but a
-- privacy obligation. ResponseText below is a verbatim candidate-model response body, held under
-- the CTO-125 carve-out to the no-bodies invariant, and the consent text scopes that body to
-- retention_days. A body kept past the horizon the tenant agreed to is the carve-out quietly
-- widening itself, so this TTL must never be longer than replay_samples'.
TTL toDateTime(RanAt) + INTERVAL 30 DAY DELETE;


-- CTO-125 additive migration. Idempotent: `ADD COLUMN IF NOT EXISTS` plus defaults so existing
-- rows (which predate candidate-response persistence) read back as empty strings and the eval
-- judge falls back to the envelope re-render path for them. Same style as the otel_spans ALTERs.
--
-- PII CARVE-OUT: ResponseText is the verbatim candidate-model response body. The span-side guard
-- (mapping.py `_is_body_key`) deliberately refuses message bodies in `otel_spans`/business events.
-- Replay is a *separate, opt-in* path (Postgres `tenant_replay_config`, default OFF) with its own
-- retention TTL (`retention_days`) and access tier. Persisting the body here lets the pairwise LLM
-- judge grade the candidate's ACTUAL output instead of a reconstruction. This does NOT relax the
-- span-side no-bodies invariant; it applies only to this opt-in replay table.
ALTER TABLE replay_runs
    ADD COLUMN IF NOT EXISTS ResponseText  String  DEFAULT '' CODEC(ZSTD(1)),
    ADD COLUMN IF NOT EXISTS FinishReason  LowCardinality(String) DEFAULT '';
