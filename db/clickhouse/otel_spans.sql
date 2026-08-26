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

    -- Account dimension (CTO-180). The tenant's own paying customer: the company, workspace or
    -- team that the end user belongs to. It sits between TenantId (the ai-tally customer) and
    -- UserIdHash (an individual end user), which is a gap nothing filled before. Cost per USER
    -- already works because UserIdHash is on every span; cost per CUSTOMER needs this grouping.
    --
    -- Same type and same treatment as UserIdHash: HMAC-SHA256 hex under the per-tenant key, so
    -- an account hash cannot be reversed and cannot be joined across tenants. Raw account ids
    -- never reach this table. AccountIdHashKeyVersion mirrors UserIdHashKeyVersion so an account
    -- survives a key rotation the same way a user does (CTO-74).
    --
    -- DEFAULT '' is load-bearing. Every span written before this column existed, and every span
    -- from a tenant that has not instrumented account_id, reads back as ''. That is the
    -- UNATTRIBUTED bucket and callers must render it as such: it is not a customer named
    -- "unknown", and it must never be ranked alongside real accounts.
    --
    -- There is deliberately NO account label / display name column here. A label is mutable
    -- metadata: stamping it on every span wastes storage and creates a "which label wins"
    -- question the moment an account is renamed. It would also put customer names in the
    -- telemetry store, which is precisely what hashing the id exists to prevent. Labels live in
    -- the Postgres control plane, keyed on this hash and joined at render time.
    AccountIdHash          FixedString(64) DEFAULT ''  CODEC(ZSTD(1)),  -- HMAC-SHA256 hex
    AccountIdHashKeyVersion LowCardinality(String),                     -- HMAC rotation (CTO-74)
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

    -- Stratified-sampling provenance (CTO-119). SamplingStratum is the head-time classification
    -- ('body'|'mid'|'tail'); SamplingRate is THAT stratum's configured keep rate. Distinct from
    -- the per-span `SampleRate` weight below (which is the billing-extrapolation factor — they're
    -- usually equal today, but conceptually independent: rate can change after a span is kept).
    -- Default 'unsampled' / 1.0 so pre-CTO-119 rows group as a separate, honestly-labelled bucket.
    SamplingStratum        LowCardinality(String) DEFAULT 'unsampled',
    SamplingRate           Float32 DEFAULT 1.0     CODEC(ZSTD(1)),

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

-- CTO-119 additive migration. Same idempotent pattern; default 'unsampled' / 1.0 means
-- pre-migration rows group as their own bucket on the DQ surface rather than polluting
-- body/mid/tail breakdowns.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS SamplingStratum LowCardinality(String) DEFAULT 'unsampled',
    ADD COLUMN IF NOT EXISTS SamplingRate    Float32                DEFAULT 1.0 CODEC(ZSTD(1));

-- CTO-180 additive migration. Same idempotent pattern as CTO-118/CTO-119: `ADD COLUMN IF NOT
-- EXISTS` plus a DEFAULT makes this metadata-only, so it applies to an already-populated table
-- without rewriting a single part and without blocking ingest. There is no backfill step because
-- there is nothing to backfill from: no span ever carried an account id, so historical rows stay
-- '' and are reported as unattributed rather than guessed at.
--
-- APPLYING THIS TO AN EXISTING DEPLOYMENT. The compose initdb directory that mounts this file
-- runs ONLY on a first boot against an empty volume, so a stack that is already up will never
-- see the statement below on its own. Replay the canonical DDL with `make ch-migrate` from
-- infra/ (every statement in db/clickhouse is IF NOT EXISTS, so replaying is safe and repeatable).
-- This is not a hypothetical: the Postgres side of this repo has already shipped migrations
-- (0011, 0012, 0015, 0016) that silently never reached an existing volume for exactly this reason.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS AccountIdHash           FixedString(64) DEFAULT '' CODEC(ZSTD(1)),
    ADD COLUMN IF NOT EXISTS AccountIdHashKeyVersion LowCardinality(String);
