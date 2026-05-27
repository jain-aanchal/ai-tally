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
    LastTraceCost        Decimal64(8),
    UpdatedAt            DateTime64(9) DEFAULT now64()
)
ENGINE = ReplacingMergeTree(UpdatedAt)
ORDER BY (TenantId, UserIdHash, FeatureTag);

CREATE MATERIALIZED VIEW IF NOT EXISTS last_touch_index_mv
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
