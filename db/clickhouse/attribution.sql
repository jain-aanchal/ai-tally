-- Attribution tables (Workflow 2). Implements CTO-26. Spec §5.1, §7.
-- TenantId-first throughout (shared multi-tenant). Carries UserIdHashKeyVersion for cross-version
-- identity bridging (CTO-74). Defined pre-data — adding columns later is a backfill incident.

-- identity_graph: transitive identity edges (anonymous <-> user <-> session, across key versions).
CREATE TABLE IF NOT EXISTS identity_graph
(
    TenantId              LowCardinality(String),
    IdentityA             FixedString(64),
    IdentityAType         Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5),
    IdentityB             FixedString(64),
    IdentityBType         Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5),
    UserIdHashKeyVersion  LowCardinality(String),
    Confidence            Float32,
    ObservedAt            DateTime64(9),
    Source                LowCardinality(String),
    INDEX idx_a IdentityA TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_b IdentityB TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ObservedAt)
ORDER BY (TenantId, IdentityA, IdentityB, Source);

-- business_events: inbound value events from CDPs/webhooks.
CREATE TABLE IF NOT EXISTS business_events
(
    TenantId          LowCardinality(String),
    BusinessEventId   String,
    EventName         LowCardinality(String),
    UserIdHash        FixedString(64),
    -- Account dimension (CTO-180). Mirrors otel_spans.AccountIdHash: the tenant's own paying
    -- customer, HMAC-SHA256 hex under the per-tenant key, never the raw id and never a name.
    -- Value events need it for the same reason spans do. Revenue arrives per ACCOUNT (a
    -- subscription, a contract), so margin per customer is only answerable if both sides of the
    -- join carry the account. DEFAULT '' means every event written before this column existed
    -- reads back as unattributed, which is a fact about our instrumentation and not a customer.
    AccountIdHash     FixedString(64) DEFAULT '',
    OccurredAt        DateTime64(9),
    IngestedAt        DateTime64(9),
    ValueAmountMicro  Nullable(Int64),
    ValueCurrency     LowCardinality(String),
    ValueType         Enum8('monetary'=1,'count'=2,'mrr'=3,'refund'=4),
    Source            LowCardinality(String),
    RawPayload        String CODEC(ZSTD(3))
)
ENGINE = ReplacingMergeTree(IngestedAt)
PARTITION BY toYYYYMM(OccurredAt)
ORDER BY (TenantId, BusinessEventId);

-- attribution_records: idempotent on (TenantId, BusinessEventId, FeatureTag).
CREATE TABLE IF NOT EXISTS attribution_records
(
    TenantId              LowCardinality(String),
    BusinessEventId       String,
    FeatureTag            LowCardinality(String),
    AttributedTraceId     String,
    AttributedTraceTs     DateTime64(9),
    AttributedTraceCost   Decimal64(8),
    ValueAmountMicro      Nullable(Int64),
    ValueCurrency         LowCardinality(String),
    AttributionModel      LowCardinality(String),
    AttributionConfidence Enum8('direct'=1,'session_stitched'=2,'identity_graph_stitched'=3),
    UserIdHashKeyVersion  LowCardinality(String),
    LookbackWindowDays    UInt16,
    StitchedAt            DateTime64(9),
    StitcherVersion       LowCardinality(String)
)
ENGINE = ReplacingMergeTree(StitchedAt)
PARTITION BY toYYYYMM(AttributedTraceTs)
ORDER BY (TenantId, BusinessEventId, FeatureTag);

-- unattributed_events: queryable, NOT a silent drop. Re-checked by the reconciler.
CREATE TABLE IF NOT EXISTS unattributed_events
(
    TenantId         LowCardinality(String),
    BusinessEventId  String,
    EventName        LowCardinality(String),
    UserIdHash       FixedString(64),
    OccurredAt       DateTime64(9),
    Reason           Enum8('no_trace_in_window'=1,'unknown_user'=2,'identity_unresolved'=3,'feature_tag_missing'=4),
    LastCheckedAt    DateTime64(9)
)
ENGINE = ReplacingMergeTree(LastCheckedAt)
ORDER BY (TenantId, BusinessEventId);

-- CTO-180 additive migration for business_events. Idempotent `ADD COLUMN IF NOT EXISTS` with a
-- DEFAULT, so it is metadata-only against an existing populated table and needs no backfill:
-- nothing ever emitted an account id, so historical events stay unattributed rather than guessed.
--
-- As with otel_spans, an existing deployment will NOT pick this up on its own. The compose initdb
-- directory that mounts this file runs only on a first boot against an empty volume. Replay the
-- canonical DDL with `make ch-migrate` from infra/ to apply it to a stack that is already running.
ALTER TABLE business_events
    ADD COLUMN IF NOT EXISTS AccountIdHash FixedString(64) DEFAULT '';
