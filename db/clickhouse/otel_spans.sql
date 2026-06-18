-- otel_spans — primary span table (ai-tally telemetry store)
-- Implements CTO-22. Spec §5.1.
--
-- Shared multi-tenant cluster (CTO-18): TenantId is FIRST in ORDER BY and is load-bearing —
-- every read must be tenant-scoped or it scans the whole cluster.
--
-- Cost is Decimal64(8) (money, never Float64). Dual-track: EstimatedCost + ReconciledCost +
-- CostSource. UserIdHashKeyVersion supports HMAC versioned-key rotation (CTO-74).
-- High-value attributes are promoted to typed columns; the long tail stays in SpanAttributes.

CREATE TABLE IF NOT EXISTS otel_spans
(
    TenantId               LowCardinality(String),
    Timestamp              DateTime64(9)            CODEC(Delta, ZSTD(1)),
    TraceId                String                   CODEC(ZSTD(1)),
    SpanId                 String                   CODEC(ZSTD(1)),
    ParentSpanId           String                   CODEC(ZSTD(1)),

    ServiceName            LowCardinality(String),
    SpanName               LowCardinality(String),
    StatusCode             UInt8,
    DurationNs             UInt64                   CODEC(T64, ZSTD(1)),

    -- Business / attribution
    FeatureTag             LowCardinality(String),
    SessionId              String                   CODEC(ZSTD(1)),
    UserIdHash             FixedString(64)          CODEC(ZSTD(1)),  -- HMAC-SHA256 hex
    UserIdHashKeyVersion   LowCardinality(String),                  -- HMAC rotation (CTO-74)
    IdempotencyKey         String                   CODEC(ZSTD(1)),

    -- GenAI core (gen_ai.* semconv)
    GenAiSystem            LowCardinality(String),
    GenAiRequestModel      LowCardinality(String),
    GenAiResponseModel     LowCardinality(String),
    GenAiOperation         LowCardinality(String),
    GenAiToolName          LowCardinality(String),
    InputTokens            UInt32                   CODEC(T64, ZSTD(1)),
    OutputTokens           UInt32                   CODEC(T64, ZSTD(1)),
    CachedInputTokens      UInt32                   CODEC(T64, ZSTD(1)),

    -- Cost (dual-track; Decimal64(8), NOT Float64)
    EstimatedCost          Decimal64(8)             CODEC(ZSTD(1)),
    ReconciledCost         Nullable(Decimal64(8))   CODEC(ZSTD(1)),
    CostCurrency           LowCardinality(String),
    CostSource             Enum8('estimated' = 1, 'reconciled' = 2),
    PriceCatalogVersion    LowCardinality(String),

    -- Agent context
    AgentRunId             String                   CODEC(ZSTD(1)),
    AgentStepIndex         UInt16,

    -- Context-window drops (CTO-118). Counts only — never the dropped message text.
    -- All three default to 0 so existing rows survive an additive ALTER without backfill.
    ContextDroppedMessages UInt32 DEFAULT 0         CODEC(T64, ZSTD(1)),
    ContextDroppedTokens   UInt32 DEFAULT 0         CODEC(T64, ZSTD(1)),
    ContextWindowUsedPct   Float32 DEFAULT 0        CODEC(ZSTD(1)),

    -- Replay (Workflow 1)
    ResolvedPromptHash     FixedString(64),
    ResolvedContextRef     String,

    -- Long tail
    SpanAttributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    SpanEvents             Array(Tuple(name String, ts DateTime64(9), attrs Map(String, String))),

    -- Sampling
    SampleRate             Float32 DEFAULT 1.0,

    INDEX idx_trace_id     TraceId                  TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_session_id   SessionId                TYPE bloom_filter(0.01)  GRANULARITY 4,
    INDEX idx_user_id      UserIdHash               TYPE bloom_filter(0.01)  GRANULARITY 4,
    INDEX idx_agent_run    AgentRunId               TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_attr_keys    mapKeys(SpanAttributes)  TYPE bloom_filter(0.01)  GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (TenantId, FeatureTag, ServiceName, SpanName, Timestamp)
-- Tiering (CTO-29): hot SSD -> warm volume at 7d -> cold volume at 30d -> drop raw at 90d.
-- This TTL is GENERATED from tally.storage_tiering.DEFAULT_POLICY (render_ttl_clause), the single
-- source of truth that also classifies a span's tier at query time, so DDL and logic can't drift.
-- NOTE: ClickHouse `TTL ... GROUP BY` requires its keys to be a prefix of the primary key, so we
-- deliberately do NOT aggregate-on-expire here (toDate(Timestamp)/GenAiResponseModel are not a PK
-- prefix). The surviving long-horizon aggregate lives in the rollup materialized views (CTO-24,
-- daily_feature_rollup), which persist independently of this raw table's retention — so trends and
-- late billing true-ups keep working after the raw span is dropped. Storage volumes ('warm',
-- 'cold') are configured in the ClickHouse storage policy (infra, CTO-94). Per-tenant retention
-- overrides (enterprise = longer) compile to a multiIf DELETE expression — see storage_tiering.sql.
TTL
    toDateTime(Timestamp) + INTERVAL 7 DAY  TO VOLUME 'warm',
    toDateTime(Timestamp) + INTERVAL 30 DAY TO VOLUME 'cold',
    toDateTime(Timestamp) + INTERVAL 90 DAY DELETE;

-- CTO-118 additive migration. Idempotent: `ADD COLUMN IF NOT EXISTS` plus a `DEFAULT 0`
-- so the operation is metadata-only and non-blocking; existing rows show 0 until they're
-- naturally aged out. Counts only — there is no body field here, and never will be.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS ContextDroppedMessages UInt32  DEFAULT 0 CODEC(T64, ZSTD(1)),
    ADD COLUMN IF NOT EXISTS ContextDroppedTokens   UInt32  DEFAULT 0 CODEC(T64, ZSTD(1)),
    ADD COLUMN IF NOT EXISTS ContextWindowUsedPct   Float32 DEFAULT 0 CODEC(ZSTD(1));
