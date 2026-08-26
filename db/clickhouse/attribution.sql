-- Attribution tables (Workflow 2). Implements CTO-26. Spec §5.1, §7.
-- TenantId-first throughout (shared multi-tenant). Carries UserIdHashKeyVersion for cross-version
-- identity bridging (CTO-74). Defined pre-data — adding columns later is a backfill incident.

-- identity_graph: transitive identity edges (anonymous <-> user <-> session, across key versions).
--
-- 'account_id' (CTO-184) is the sixth identity type. It is the stitching path for the account
-- dimension CTO-180 added to otel_spans / business_events: a tenant who cannot stamp an
-- `account_id` on every span can instead let a CRM or CDP connector assert `user_id <-> account_id`
-- here, and the account is inferred from the user at attribution time.
--
-- APPEND ONLY, NEVER RENUMBER. ClickHouse stores an Enum8 as its integer, not its name, so
-- changing 'email'=4 to anything else would silently reinterpret every row already on disk as a
-- different identity type. New values therefore take the next free ordinal (6) and existing
-- ordinals 1-5 are frozen forever. The same rule applies to whatever comes after this.
CREATE TABLE IF NOT EXISTS identity_graph
(
    TenantId              LowCardinality(String),
    IdentityA             FixedString(64),
    IdentityAType         Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5,'account_id'=6),
    IdentityB             FixedString(64),
    IdentityBType         Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5,'account_id'=6),
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

-- CTO-184 additive migration for identity_graph. Widening an Enum8 is NOT an ADD COLUMN, so the
-- IF NOT EXISTS trick above does not apply; the idempotent form is MODIFY COLUMN to the full
-- target type. Restating a type ClickHouse already has is a no-op that costs one metadata write,
-- which is what makes replaying this file through `make ch-migrate` safe and repeatable.
--
-- This is metadata-only and does NOT rewrite parts, for one specific reason: every pre-existing
-- name keeps its pre-existing ordinal. An Enum8 column is stored on disk as the Int8 ordinal, and
-- the name is only a display mapping held in the table metadata. Appending 'account_id'=6 leaves
-- every byte already written meaning exactly what it meant before. Renumbering, or reusing an
-- ordinal for a different name, would instead silently reinterpret stored rows with no error and
-- no way to tell after the fact, so it is not something a later migration may do either.
--
-- As with CTO-180, an existing deployment will NOT pick this up on its own: the compose initdb
-- directory that mounts this file runs only on a first boot against an empty volume. Replay the
-- canonical DDL with `make ch-migrate` from infra/ to apply it to a stack that is already running.
ALTER TABLE identity_graph
    MODIFY COLUMN IdentityAType
        Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5,'account_id'=6);

ALTER TABLE identity_graph
    MODIFY COLUMN IdentityBType
        Enum8('user_id'=1,'anonymous_id'=2,'session_id'=3,'email'=4,'external_id'=5,'account_id'=6);
