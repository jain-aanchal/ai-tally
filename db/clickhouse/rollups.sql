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
    -- CTO-244 coverage counters. otel_spans.InputTokens/OutputTokens/EstimatedCost are Nullable:
    -- "the provider never told us" is a real state and it is not 0. The sums above therefore cover
    -- only the spans we actually know, which makes them a LOWER BOUND, not a total. These two
    -- counters are what stops that from being silent: a reader that finds them non-zero must say
    -- the figure is partial (and blank any per-call or per-token derivation from it) rather than
    -- present an under-count as complete. They are plain UInt64 so SummingMergeTree adds them the
    -- same way it adds SpanCount.
    UnknownUsageSpanCount UInt64,
    UnpricedSpanCount     UInt64,
    TraceCountState     AggregateFunction(uniq, String),
    UserCountState      AggregateFunction(uniq, FixedString(64))
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Day)
ORDER BY (TenantId, FeatureTag, GenAiResponseModel, Day);


-- CTO-244 migration for an EXISTING deployment. Two steps, and both are needed.
--
-- 1. The CREATE TABLE above is IF NOT EXISTS, so it is a no-op on a stack that already has these
--    tables. Add the coverage counters explicitly. `AFTER SpanCount` keeps the physical column
--    order identical to the CREATE TABLE, and DEFAULT 0 makes it metadata-only: rows rolled up
--    before this change read 0 unknown, which is not a claim that they had no unknowns. It is a
--    claim that nobody counted, because before the cutover an unknown was indistinguishable from a
--    real zero. Pre-cutover rollup figures may understate spend and cannot be corrected. See the
--    CTO-244 note in otel_spans.sql and RUNNING.md.
-- 2. A materialized view's SELECT cannot be ALTERed, and `CREATE ... IF NOT EXISTS` will not
--    replace one that already exists, so a replay would leave the old definition writing the old
--    columns forever. The MVs below are therefore DROPped and recreated on every replay. Dropping
--    a `TO`-table MV does not touch the target table, so no history is lost; the only cost is that
--    spans inserted during the drop/create window are not rolled up, which is why ch-migrate is an
--    operator action rather than something on the ingest path.
ALTER TABLE daily_feature_rollup
    ADD COLUMN IF NOT EXISTS UnknownUsageSpanCount UInt64 DEFAULT 0 AFTER SpanCount,
    ADD COLUMN IF NOT EXISTS UnpricedSpanCount     UInt64 DEFAULT 0 AFTER UnknownUsageSpanCount;

DROP VIEW IF EXISTS daily_feature_rollup_mv;
CREATE MATERIALIZED VIEW daily_feature_rollup_mv
TO daily_feature_rollup
AS SELECT
    TenantId,
    toDate(Timestamp)                      AS Day,
    FeatureTag,
    GenAiResponseModel,
    -- CTO-244: sum() already skips NULLs. ifNull sits OUTSIDE the aggregate (an all-NULL group sums
    -- to NULL) so the result stays non-Nullable for the SummingMergeTree target column; wrapping the
    -- column instead trips ClickHouse's nested-aggregate check. The `otel_spans.` qualifier is
    -- required for the same reason: the output alias shadows the column name, so an unqualified
    -- reference inside the aggregate resolves to the alias and ClickHouse sees sum() inside sum(). The honesty lives in the two counters below, not in the sum.
    ifNull(sum(otel_spans.InputTokens), 0)            AS InputTokens,
    ifNull(sum(otel_spans.OutputTokens), 0)           AS OutputTokens,
    ifNull(sum(otel_spans.CachedInputTokens), 0)      AS CachedInputTokens,
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                AS SpanCount,
    -- CTO-244 (follow-up): "unknown usage" is per operation kind, and it is only unknown when it
    -- actually stopped us pricing the span. `InputTokens IS NULL OR OutputTokens IS NULL` was
    -- written for the chat path and over-reported everywhere else: an embedding call has no output
    -- side by design, and tool / vector / compute / egress spans are priced per call and carry no
    -- token counts at all, so three correctly priced layers were being flagged unknown-usage. On a
    -- live stack that read 5 of 8 spans unknown when 1 genuinely was, 4 of them with a real priced
    -- cost. The `EstimatedCost IS NULL` conjunct makes the invariant structural rather than
    -- incidental: a span counted here can never also carry a priced cost. A span whose usage is
    -- known but whose model is not in the catalog is unpriced-but-not-unknown-usage, so it lands in
    -- UnpricedSpanCount alone, which is the distinction these two counters exist to draw.
    -- Getting this right at INSERT time matters: these are SummingMergeTree targets, so a wrong
    -- count is added into the sum forever and no later merge can repair it.
    countIf(
        otel_spans.EstimatedCost IS NULL
        AND multiIf(
            otel_spans.GenAiOperation = 'embeddings', otel_spans.InputTokens IS NULL,
            otel_spans.GenAiOperation IN ('tool', 'vector', 'compute', 'egress'), 0,
            otel_spans.InputTokens IS NULL OR otel_spans.OutputTokens IS NULL
        )
    ) AS UnknownUsageSpanCount,
    countIf(otel_spans.EstimatedCost IS NULL) AS UnpricedSpanCount,
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
    -- CTO-244 coverage counters. otel_spans.InputTokens/OutputTokens/EstimatedCost are Nullable:
    -- "the provider never told us" is a real state and it is not 0. The sums above therefore cover
    -- only the spans we actually know, which makes them a LOWER BOUND, not a total. These two
    -- counters are what stops that from being silent: a reader that finds them non-zero must say
    -- the figure is partial (and blank any per-call or per-token derivation from it) rather than
    -- present an under-count as complete. They are plain UInt64 so SummingMergeTree adds them the
    -- same way it adds SpanCount.
    UnknownUsageSpanCount UInt64,
    UnpricedSpanCount     UInt64,
    TraceCountState     AggregateFunction(uniq, String),
    UserCountState      AggregateFunction(uniq, FixedString(64))
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Hour)
ORDER BY (TenantId, FeatureTag, GenAiResponseModel, Hour);

-- CTO-244 migration for an EXISTING deployment, the hourly half of the two steps documented above
-- the daily ALTER. It must sit BELOW the CREATE TABLE it alters: the ClickHouse initdb entrypoint
-- runs this file top to bottom against an EMPTY database, so an ALTER placed above the CREATE hits
-- UNKNOWN_TABLE and aborts the whole boot. Below the CREATE it is correct both ways round: a no-op
-- on a fresh database (the columns are already in the CREATE) and the actual migration on replay.
ALTER TABLE hourly_feature_rollup
    ADD COLUMN IF NOT EXISTS UnknownUsageSpanCount UInt64 DEFAULT 0 AFTER SpanCount,
    ADD COLUMN IF NOT EXISTS UnpricedSpanCount     UInt64 DEFAULT 0 AFTER UnknownUsageSpanCount;

DROP VIEW IF EXISTS hourly_feature_rollup_mv;
CREATE MATERIALIZED VIEW hourly_feature_rollup_mv
TO hourly_feature_rollup
AS SELECT
    TenantId,
    toStartOfHour(Timestamp)               AS Hour,
    FeatureTag,
    GenAiResponseModel,
    -- CTO-244: sum() already skips NULLs. ifNull sits OUTSIDE the aggregate (an all-NULL group sums
    -- to NULL) so the result stays non-Nullable for the SummingMergeTree target column; wrapping the
    -- column instead trips ClickHouse's nested-aggregate check. The `otel_spans.` qualifier is
    -- required for the same reason: the output alias shadows the column name, so an unqualified
    -- reference inside the aggregate resolves to the alias and ClickHouse sees sum() inside sum(). The honesty lives in the two counters below, not in the sum.
    ifNull(sum(otel_spans.InputTokens), 0)            AS InputTokens,
    ifNull(sum(otel_spans.OutputTokens), 0)           AS OutputTokens,
    ifNull(sum(otel_spans.CachedInputTokens), 0)      AS CachedInputTokens,
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                AS SpanCount,
    -- Same per-operation predicate as the daily MV above, and for the same reason: see the long
    -- CTO-244 follow-up note there. Both MVs write the same counter, so they must agree exactly or
    -- the hourly and daily views of one day disagree about how much of it we could price.
    countIf(
        otel_spans.EstimatedCost IS NULL
        AND multiIf(
            otel_spans.GenAiOperation = 'embeddings', otel_spans.InputTokens IS NULL,
            otel_spans.GenAiOperation IN ('tool', 'vector', 'compute', 'egress'), 0,
            otel_spans.InputTokens IS NULL OR otel_spans.OutputTokens IS NULL
        )
    ) AS UnknownUsageSpanCount,
    countIf(otel_spans.EstimatedCost IS NULL) AS UnpricedSpanCount,
    uniqState(TraceId)                     AS TraceCountState,
    uniqState(UserIdHash)                  AS UserCountState
FROM otel_spans
GROUP BY TenantId, Hour, FeatureTag, GenAiResponseModel;
