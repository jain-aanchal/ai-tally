-- Rollup materialized views (ai-tally telemetry store)
-- Implements CTO-24. Spec §5.1, Appendix A.
--
-- CRITICAL PATH: dashboard queries read these rollups, never raw otel_spans. SummingMergeTree
-- aggregates per (TenantId, FeatureTag, GenAiResponseModel, bucket). Long-horizon aggregates live
-- here (they persist independently of otel_spans' 90d retention, CTO-22/CTO-29).
--
-- uniqState/sumState are AggregateFunction states; query with -Merge combinators.

CREATE TABLE IF NOT EXISTS daily_feature_rollup
(
    TenantId            LowCardinality(String),
    Day                 Date,
    FeatureTag          LowCardinality(String),
    GenAiResponseModel  LowCardinality(String),
    InputTokens         UInt64,
    OutputTokens        UInt64,
    CachedInputTokens   UInt64,
    EstimatedCost       Decimal64(8),
    ReconciledCost      Decimal64(8),
    SpanCount           UInt64,
    TraceCountState     AggregateFunction(uniq, String),
    UserCountState      AggregateFunction(uniq, FixedString(64))
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Day)
ORDER BY (TenantId, FeatureTag, GenAiResponseModel, Day);

CREATE MATERIALIZED VIEW IF NOT EXISTS daily_feature_rollup_mv
TO daily_feature_rollup
AS SELECT
    TenantId,
    toDate(Timestamp)                      AS Day,
    FeatureTag,
    GenAiResponseModel,
    sum(InputTokens)                       AS InputTokens,
    sum(OutputTokens)                      AS OutputTokens,
    sum(CachedInputTokens)                 AS CachedInputTokens,
    sum(EstimatedCost)                     AS EstimatedCost,
    sum(ifNull(ReconciledCost, toDecimal64(0, 8))) AS ReconciledCost,
    count()                                AS SpanCount,
    uniqState(TraceId)                     AS TraceCountState,
    uniqState(UserIdHash)                  AS UserCountState
FROM otel_spans
GROUP BY TenantId, Day, FeatureTag, GenAiResponseModel;

-- Hourly rollup: same shape, finer bucket. Powers "last hour" views without scanning raw spans.
CREATE TABLE IF NOT EXISTS hourly_feature_rollup
(
    TenantId            LowCardinality(String),
    Hour                DateTime,
    FeatureTag          LowCardinality(String),
    GenAiResponseModel  LowCardinality(String),
    InputTokens         UInt64,
    OutputTokens        UInt64,
    CachedInputTokens   UInt64,
    EstimatedCost       Decimal64(8),
    ReconciledCost      Decimal64(8),
    SpanCount           UInt64,
    TraceCountState     AggregateFunction(uniq, String),
    UserCountState      AggregateFunction(uniq, FixedString(64))
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Hour)
ORDER BY (TenantId, FeatureTag, GenAiResponseModel, Hour);

CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_feature_rollup_mv
TO hourly_feature_rollup
AS SELECT
    TenantId,
    toStartOfHour(Timestamp)               AS Hour,
    FeatureTag,
    GenAiResponseModel,
    sum(InputTokens)                       AS InputTokens,
    sum(OutputTokens)                      AS OutputTokens,
    sum(CachedInputTokens)                 AS CachedInputTokens,
    sum(EstimatedCost)                     AS EstimatedCost,
    sum(ifNull(ReconciledCost, toDecimal64(0, 8))) AS ReconciledCost,
    count()                                AS SpanCount,
    uniqState(TraceId)                     AS TraceCountState,
    uniqState(UserIdHash)                  AS UserCountState
FROM otel_spans
GROUP BY TenantId, Hour, FeatureTag, GenAiResponseModel;
