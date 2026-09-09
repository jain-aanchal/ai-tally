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
ORDER BY (TenantId, IdentityA, IdentityB, Source)
-- Retention (CTO-338): OPERATIONAL GRAIN, 13 months. An identity edge exists to let a value event
-- find a past touch; once attribution_records holds the result, the edge has done its job and the
-- answer it produced is preserved for 7 years without it. It is also hashed personal data, so the
-- shorter horizon is the privacy-preferable one and not only the cheaper one. 13 months is well
-- clear of any lookback window (default 30 days) plus reconciler re-checks.
TTL toDateTime(ObservedAt) + INTERVAL 400 DAY DELETE;

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
    -- CTO-338 column TTL: see note 2 in the retention comment below the column list. Resets this
    -- column to '' at 90 days; the row and every monetary column on it stay for the full 7 years.
    RawPayload        String CODEC(ZSTD(3)) TTL toDateTime(OccurredAt) + INTERVAL 90 DAY
)
ENGINE = ReplacingMergeTree(IngestedAt)
PARTITION BY toYYYYMM(OccurredAt)
ORDER BY (TenantId, BusinessEventId)
-- Retention (CTO-338), and this one is two decisions, not one.
--
-- 1. THE ROW: BOOK OF RECORD, 7 years. This is inbound customer revenue. It is not derived from
--    anything we hold and we cannot re-derive it; re-ingesting from the source connector is a
--    best-effort favour, not a guarantee. More importantly, revenue and ROI figures are read off
--    windows that reach into the past, so a shorter horizon would make a number a customer already
--    read get SMALLER on a later page load with nothing on screen saying why. That is the same
--    class of silent-restatement problem as the attribution fan-out fixed in CTO-346, and it is
--    why finance-adjacent data gets a retention FLOOR rather than a ceiling. Seven years is the
--    books-and-records horizon; attribution_records is pinned to the identical number so the two
--    sides of the ROI join can never disagree about which periods exist.
--
-- 2. THE PAYLOAD: RawPayload expires at 90 days on its own COLUMN TTL, declared on the column
--    below. A column TTL resets the column to its default and keeps the row, so the money survives
--    for 7 years and only the verbatim inbound webhook body behind it expires. That split is what
--    makes the long row horizon affordable: RawPayload is ZSTD(3) blob text and is the bulk of this
--    table's 69 MiB, while the columns that answer a revenue question are a handful of scalars. 90
--    days matches the raw span horizon because the payload plays the same role for a value event
--    that a raw span plays for a cost: the artifact you debug a mapping against, not the record.
--    It is also unmapped third-party text, so expiring it shrinks the PII surface.
TTL toDateTime(OccurredAt) + INTERVAL 2555 DAY DELETE;

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
ORDER BY (TenantId, BusinessEventId, FeatureTag)
-- Retention (CTO-338): BOOK OF RECORD, 7 years, and PINNED EQUAL to business_events rather than
-- merely long. The two horizons must match in both directions. Shorter here and revenue whose
-- attribution has aged out reads back as unattributed, which turns a feature that paid for itself
-- into one that did not, on a chart nobody re-checked. Longer here and an attribution outlives the
-- event it explains, leaving attributed revenue with no revenue behind it. tally.storage_tiering
-- asserts the equality at import so the pair cannot be edited apart.
--
-- Once otel_spans drops the span at 90 days, AttributedTraceCost here is the only surviving record
-- of what that conversion cost, exactly as the rollups are for aggregate spend.
TTL toDateTime(AttributedTraceTs) + INTERVAL 2555 DAY DELETE;

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
ORDER BY (TenantId, BusinessEventId)
-- Retention (CTO-338): OPERATIONAL GRAIN, 13 months. This is the reconciler's re-check queue, not
-- a record of money: the revenue itself is in business_events for 7 years either way. An event
-- still unattributed after 13 months is not going to become attributed, and dropping the queue
-- entry does not make the event disappear from the unattributed-revenue total, which is derived
-- from business_events. No PARTITION BY here either, so the same part-rewrite cost note as
-- last_touch_index applies, at a far smaller scale.
TTL toDateTime(OccurredAt) + INTERVAL 400 DAY DELETE;

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
