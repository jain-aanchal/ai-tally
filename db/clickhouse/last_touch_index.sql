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
ORDER BY (TenantId, UserIdHash, FeatureTag);

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
