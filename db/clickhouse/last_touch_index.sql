-- last_touch_index — O(1) "most recent trace per (tenant, user, feature)" for the stitcher.
-- Implements CTO-25. Spec §5.1, §7.
--
-- ReplacingMergeTree keyed (TenantId, UserIdHash, FeatureTag); newest UpdatedAt wins. Carries
-- UserIdHashKeyVersion so cross-version identity bridging works after HMAC rotation (CTO-74).
-- Query with FINAL (or argMax) to collapse to the latest row.

CREATE TABLE IF NOT EXISTS last_touch_index
(
    TenantId             LowCardinality(String),
    UserIdHash           FixedString(64),
    FeatureTag           LowCardinality(String),
    UserIdHashKeyVersion LowCardinality(String),
    LastTraceId          String,
    LastTraceTs          DateTime64(9),
    -- CTO-244: Nullable because otel_spans.EstimatedCost is. This column is a single span's cost,
    -- not a sum, so there is nothing to fall back to: if that call could not be priced, the honest
    -- value is NULL and the reader renders a blank with a reason. A 0 here would assert the user's
    -- last touch was free.
    LastTraceCost        Nullable(Decimal64(8)),
    UpdatedAt            DateTime64(9) DEFAULT now64()
)
ENGINE = ReplacingMergeTree(UpdatedAt)
ORDER BY (TenantId, UserIdHash, FeatureTag)
-- Retention (CTO-338): FOLLOWS RAW SPANS, 90 days, the otel_spans horizon exactly. This was the
-- largest table in the database (89 MiB against the raw span table's 49 MiB) and it is an index,
-- not a record: every row is a pointer at one otel_spans row. When TTL drops that span the pointer
-- dangles, so keeping the index longer than the spans keeps nothing useful and costs the most
-- storage of anything here.
--
-- The 90 is a reference to tally.storage_tiering.FOLLOWS_RAW_SPANS_DAYS (= DEFAULT_COLD_DAYS), not
-- a coincidence, and storage_tiering enforces that they stay equal. A tenant with a longer raw
-- retention override needs the SAME override here, compiled into the same multiIf: see
-- storage_tiering.sql. Applying only one of the two would silently break stitching for that tenant.
--
-- CONSTRAINT AN OPERATOR CAN BREAK: the stitcher loads touches over [now - 2*lookback, now]
-- (gateway/stitcher_job.py), with lookback defaulting to 30 days, so it reads at most 60 days back
-- and 90 leaves a month of headroom. Raising a tenant's value_events.lookback_days above HALF the
-- raw retention silently starts attributing against a truncated index. Raise this horizon with it.
--
-- NOTE FOR THE MIGRATION: this table has no PARTITION BY, so TTL expiry here rewrites whole parts
-- rather than dropping partitions. It is the most expensive of these TTLs to apply and the one to
-- stage most carefully. See db/clickhouse/migrations/derived_table_retention.sql.
TTL toDateTime(UpdatedAt) + INTERVAL 90 DAY DELETE;

-- CTO-244 migration for an EXISTING deployment: widen the column in place (a no-op when it is
-- already Nullable, so replaying stays idempotent) and recreate the MV, whose SELECT cannot be
-- ALTERed and which CREATE ... IF NOT EXISTS would silently leave on the old definition. Dropping
-- a `TO`-table MV does not touch last_touch_index itself.
ALTER TABLE last_touch_index
    MODIFY COLUMN LastTraceCost Nullable(Decimal64(8));

DROP VIEW IF EXISTS last_touch_index_mv;
CREATE MATERIALIZED VIEW last_touch_index_mv
TO last_touch_index
AS SELECT
    TenantId,
    UserIdHash,
    FeatureTag,
    UserIdHashKeyVersion,
    TraceId        AS LastTraceId,
    Timestamp      AS LastTraceTs,
    EstimatedCost  AS LastTraceCost,
    Timestamp      AS UpdatedAt
FROM otel_spans
WHERE UserIdHash != '';
