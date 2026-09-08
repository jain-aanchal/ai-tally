-- CTO-311: is this deployment's rollup spend inflated by duplicated spans, and by how much?
-- READ-ONLY. Run it with `make ch-rollup-check` from infra/. It writes nothing.
--
-- WHY THIS EXISTS. daily_feature_rollup_mv, hourly_feature_rollup_mv (rollups.sql) and
-- daily_account_rollup_mv (account_rollups.sql) sum each span into a SummingMergeTree target at
-- INSERT time. A duplicate that reaches ClickHouse is added there before any engine sees it, and no
-- later merge subtracts it: not the ReplacingMergeTree collapse on otel_spans (CTO-245), not
-- OPTIMIZE FINAL, not anything. So a deployment that ever accepted a replayed batch carries that
-- money in its rollups forever, and the dashboard, which reads rollups and never raw spans, shows
-- it. Nothing in the data says so. This check is what makes it visible.
--
-- WHAT "TRUTH" MEANS HERE. otel_spans is a ReplacingMergeTree whose sorting key ends in
-- TraceId, SpanId, so `FROM otel_spans FINAL` is the deduplicated span set: exactly one row per
-- (TenantId, ..., Timestamp, TraceId, SpanId). Re-aggregating that at each rollup's own grain
-- reproduces what the MV would have written had every span been inserted exactly once. Any
-- difference from what the rollup actually holds is drift.
--
-- Drift is reported in BOTH directions and they have different causes:
--   rollup > raw   duplicates summed in at insert time. This is CTO-311 proper.
--   rollup < raw   spans that reached otel_spans while the MV was not attached. `make ch-migrate`
--                  DROPs and recreates all three MVs (a materialized view's SELECT cannot be
--                  ALTERed), and anything ingested inside that window lands in the raw table only.
-- The rebuild in db/clickhouse/migrations/rollup_rebuild_from_spans.sql repairs both, because it
-- does not adjust the rollup, it re-derives it.
--
-- DERIVABILITY IS THE WHOLE POINT, so it is a reported column and not an assumption.
-- otel_spans drops raw rows at 90 days (CTO-22/CTO-29, and per-tenant overrides in
-- storage_tiering.sql can make that shorter). The rollups deliberately have no TTL: they are the
-- surviving long-horizon aggregate, and history older than raw retention exists ONLY there. A
-- rollup grain whose raw spans are gone cannot be checked against anything and cannot be rebuilt
-- from anything. It is reported as `not_derivable` and the rebuild leaves it byte-for-byte alone.
-- It is NOT reported as drift, and no number is invented for it. A `not_derivable` count above zero
-- is normal on any install older than its retention window; it means "the rollups are the only
-- record of this period", which is what they are for.
--
-- Derivability is judged per (TenantId, Day) rather than per rollup grain, and it is judged against
-- the RETENTION FLOOR, not merely against "raw still has a row for this day". The difference matters
-- and an earlier revision of this file got it wrong in prose that reviewers then trusted, so it is
-- spelled out. otel_spans is PARTITION BY toDate(Timestamp), but its TTL DELETE is a PER-ROW
-- expression (and storage_tiering.sql's per-tenant override compiles to a per-row multiIf on top of
-- it). ClickHouse only drops whole partitions on expiry when ttl_only_drop_parts = 1; at the default
-- 0 it expires INDIVIDUAL ROWS during merges. So on the one day that straddles the retention
-- boundary, the morning can already be gone while the afternoon survives, and a day CAN be
-- half-present. Judging on "raw has a row here" would call such a day derivable, compare a rollup
-- against a truncated truth, report the missing morning as drift, and invite the rebuild to delete
-- money that exists nowhere else.
--
-- So a day counts as derivable only if raw still holds rows for it AND it is not at or within one
-- day of the moment its rows become eligible for deletion. That moment is read from
-- system.parts.delete_ttl_info_min, which is ClickHouse's own evaluation of the DELETE TTL over the
-- rows of each part, so it is exact for the default policy and for any per-tenant override without
-- this file parsing or assuming anything about the DDL. Since one partition is one day, a day whose
-- earliest expiry moment has arrived is treated as possibly truncated and is reported as
-- not_derivable. Partitions are per-day rather than per (tenant, day), so a day at risk for the
-- shortest-retention tenant is treated as at risk for every tenant on that day: that over-reports
-- uncertainty, which is the safe direction. The hourly rollup is judged on toDate(Hour) so an hour
-- inherits its day's verdict.
--
-- Money is Decimal64(8) USD on both sides of every comparison, so the equality is exact and a
-- mismatch is real, never a rounding artifact. Each drift is also shown as integer micro-USD, which
-- is the repo's canonical money unit; the Decimal is the storage form and the conversion happens
-- here at the display boundary only.

-- 0. PREFLIGHT. Every "truth" figure below is a FINAL read of otel_spans, which only deduplicates
--    if the table is a ReplacingMergeTree whose sorting key carries span identity. On a plain
--    MergeTree, FINAL is a no-op, the "truth" would still contain the duplicates, and this check
--    would cheerfully report that badly inflated rollups agree with badly inflated raw spans. Fail
--    loudly instead of reporting a false clean bill of health. If this throws, run
--    `make ch-migrate-otel-engine` first (CTO-245).
--
--    Read through scalar subqueries and with no FROM clause of its own, deliberately. A throwIf in
--    the select list of a query filtered to one table name is not evaluated at all when that name is
--    absent: zero matching rows means zero evaluations and the query SUCCEEDS. A guard that passes
--    precisely when the table it guards is missing is not a guard. A missing row yields the String
--    default '' here, which fails the condition and throws, which is the right answer.
SELECT
    (SELECT engine      FROM system.tables
      WHERE database = currentDatabase() AND name = 'otel_spans') AS engine,
    (SELECT sorting_key FROM system.tables
      WHERE database = currentDatabase() AND name = 'otel_spans') AS sorting_key,
    -- throwIf aborts the whole script when the condition holds; it returns 0 when it does not, so a
    -- printed 0 here is the check PASSING. There is no truthier return value to give it.
    throwIf(
        engine != 'ReplacingMergeTree' OR NOT endsWith(sorting_key, 'TraceId, SpanId'),
        'otel_spans is missing, or is not a ReplacingMergeTree keyed on span identity, so FINAL does not dedupe and this check cannot establish truth. Run make ch-migrate-otel-engine first (CTO-245).'
    ) AS preflight_throws_if_broken
FORMAT Vertical;

-- 1. SUMMARY: one row per rollup per class. This is the answer to "is this deployment affected, and
--    by how much". `drift_micro_usd` on the `derivable` row is the repairable money; a positive
--    figure is spend the dashboard is currently over-stating.
--
--    The comparison is a UNION ALL of the two sides re-aggregated to a common grain, not a JOIN, so
--    a grain present on only one side (a rollup grain with no surviving spans, or a day of spans the
--    MV never saw) still produces a row instead of vanishing.
--
--    The whole union is wrapped in an outer SELECT because ClickHouse binds a trailing ORDER BY to
--    the LAST branch of a UNION ALL, not to the union.
SELECT * FROM
(
SELECT
    'daily_feature_rollup' AS rollup,
    if(derivable, 'derivable', 'not_derivable') AS class,
    count() AS grains,
    countIf(rollup_spans != raw_spans OR rollup_cost != raw_cost) AS drifted_grains,
    sum(rollup_spans) AS rollup_span_total,
    sum(raw_spans) AS raw_span_total,
    sum(rollup_cost) AS rollup_cost_usd,
    sum(raw_cost) AS raw_cost_usd,
    if(derivable, toString(toInt64(round((sum(rollup_cost) - sum(raw_cost)) * 1000000))), 'n/a') AS drift_micro_usd
FROM
(
    SELECT
        TenantId,
        Day,
        sum(r_sc) AS rollup_spans,
        sum(t_sc) AS raw_spans,
        sum(r_ec) AS rollup_cost,
        sum(t_ec) AS raw_cost,
        (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2) AS derivable
    FROM
    (
        SELECT TenantId, Day, FeatureTag, GenAiResponseModel,
               toInt64(sum(SpanCount)) AS r_sc, toDecimal64(sum(EstimatedCost), 8) AS r_ec,
               toInt64(0) AS t_sc, toDecimal64(0, 8) AS t_ec
        FROM daily_feature_rollup
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT TenantId, toDate(Timestamp) AS Day, FeatureTag, GenAiResponseModel,
               toInt64(0), toDecimal64(0, 8),
               toInt64(count()), toDecimal64(ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8)), 8)
        FROM otel_spans FINAL
        GROUP BY 1, 2, 3, 4
    )
    GROUP BY TenantId, Day, FeatureTag, GenAiResponseModel
)
GROUP BY derivable

UNION ALL

SELECT
    'hourly_feature_rollup',
    if(derivable, 'derivable', 'not_derivable'),
    count(),
    countIf(rollup_spans != raw_spans OR rollup_cost != raw_cost),
    sum(rollup_spans),
    sum(raw_spans),
    sum(rollup_cost),
    sum(raw_cost),
    if(derivable, toString(toInt64(round((sum(rollup_cost) - sum(raw_cost)) * 1000000))), 'n/a')
FROM
(
    SELECT
        TenantId,
        Hour,
        sum(r_sc) AS rollup_spans,
        sum(t_sc) AS raw_spans,
        sum(r_ec) AS rollup_cost,
        sum(t_ec) AS raw_cost,
        -- Judged on the DAY the hour falls in, against the same retention floor the daily rollups
        -- use: an hour is derivable exactly when its day is.
        (TenantId, toDate(Hour)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2) AS derivable
    FROM
    (
        SELECT TenantId, Hour, FeatureTag, GenAiResponseModel,
               toInt64(sum(SpanCount)) AS r_sc, toDecimal64(sum(EstimatedCost), 8) AS r_ec,
               toInt64(0) AS t_sc, toDecimal64(0, 8) AS t_ec
        FROM hourly_feature_rollup
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT TenantId, toStartOfHour(Timestamp) AS Hour, FeatureTag, GenAiResponseModel,
               toInt64(0), toDecimal64(0, 8),
               toInt64(count()), toDecimal64(ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8)), 8)
        FROM otel_spans FINAL
        GROUP BY 1, 2, 3, 4
    )
    GROUP BY TenantId, Hour, FeatureTag, GenAiResponseModel
)
GROUP BY derivable

UNION ALL

SELECT
    'daily_account_rollup',
    if(derivable, 'derivable', 'not_derivable'),
    count(),
    countIf(rollup_spans != raw_spans OR rollup_cost != raw_cost),
    sum(rollup_spans),
    sum(raw_spans),
    sum(rollup_cost),
    sum(raw_cost),
    if(derivable, toString(toInt64(round((sum(rollup_cost) - sum(raw_cost)) * 1000000))), 'n/a')
FROM
(
    SELECT
        TenantId,
        Day,
        sum(r_sc) AS rollup_spans,
        sum(t_sc) AS raw_spans,
        sum(r_ec) AS rollup_cost,
        sum(t_ec) AS raw_cost,
        (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2) AS derivable
    FROM
    (
        SELECT TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation,
               toInt64(sum(SpanCount)) AS r_sc, toDecimal64(sum(EstimatedCost), 8) AS r_ec,
               toInt64(0) AS t_sc, toDecimal64(0, 8) AS t_ec
        FROM daily_account_rollup
        GROUP BY 1, 2, 3, 4, 5
        UNION ALL
        SELECT TenantId, toDate(Timestamp) AS Day, AccountIdHash, FeatureTag, GenAiOperation,
               toInt64(0), toDecimal64(0, 8),
               toInt64(count()), toDecimal64(ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8)), 8)
        FROM otel_spans FINAL
        GROUP BY 1, 2, 3, 4, 5
    )
    GROUP BY TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation
)
GROUP BY derivable
)
ORDER BY rollup, class
FORMAT PrettyCompact;

-- 2. WHO IS AFFECTED, on the daily feature rollup (the table the dashboard's headline spend reads).
--    Per tenant, restricted to derivable days, so every figure here is repairable by the rebuild.
--    A tenant absent from this result has no drift.
SELECT
    TenantId,
    count() AS drifted_grains,
    min(Day) AS first_day,
    max(Day) AS last_day,
    sum(rollup_spans) AS rollup_span_total,
    sum(raw_spans) AS raw_span_total,
    sum(rollup_cost) AS rollup_cost_usd,
    sum(raw_cost) AS raw_cost_usd,
    toInt64(round((sum(rollup_cost) - sum(raw_cost)) * 1000000)) AS drift_micro_usd
FROM
(
    SELECT
        TenantId,
        Day,
        sum(r_sc) AS rollup_spans,
        sum(t_sc) AS raw_spans,
        sum(r_ec) AS rollup_cost,
        sum(t_ec) AS raw_cost
    FROM
    (
        SELECT TenantId, Day, FeatureTag, GenAiResponseModel,
               toInt64(sum(SpanCount)) AS r_sc, toDecimal64(sum(EstimatedCost), 8) AS r_ec,
               toInt64(0) AS t_sc, toDecimal64(0, 8) AS t_ec
        FROM daily_feature_rollup
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT TenantId, toDate(Timestamp) AS Day, FeatureTag, GenAiResponseModel,
               toInt64(0), toDecimal64(0, 8),
               toInt64(count()), toDecimal64(ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8)), 8)
        FROM otel_spans FINAL
        GROUP BY 1, 2, 3, 4
    )
    WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)
    GROUP BY TenantId, Day, FeatureTag, GenAiResponseModel
    HAVING rollup_spans != raw_spans OR rollup_cost != raw_cost
)
GROUP BY TenantId
ORDER BY abs(drift_micro_usd) DESC
FORMAT PrettyCompact;

-- 3. WHAT THE REBUILD WILL NOT TOUCH. Rollup grains whose raw spans no longer exist: aged out under
--    the retention TTL, or written under a tenant spelling that has since been removed from the raw
--    table. These are carried across the rebuild unchanged. They may still contain duplicated money
--    from before the fix, and this check cannot tell, because the only thing that could have told is
--    gone. That is stated rather than guessed at: a wrong number written here would be worse than
--    the acknowledged uncertainty. Treat any figure covering these days as approximate.
--
--    The inner GROUP BY is not cosmetic. daily_feature_rollup is a SummingMergeTree, so one grain
--    can sit in several un-merged parts at once and a bare count() over the table counts PARTS, not
--    grains. Section 1 collapses to the sorting key before counting; without the same collapse here
--    the two sections of one report would disagree, and step 7 of the rebuild mandates comparing
--    this number before and after a run that necessarily changes the part layout. sum(SpanCount)
--    and sum(EstimatedCost) are unaffected, since summing the duplicate parts is the whole point of
--    the engine; only count() had to be fixed.
SELECT
    TenantId,
    count() AS not_derivable_grains,
    min(Day) AS first_day,
    max(Day) AS last_day,
    sum(SpanCount) AS span_total,
    sum(EstimatedCost) AS cost_usd,
    toInt64(round(sum(EstimatedCost) * 1000000)) AS cost_micro_usd
FROM
(
    SELECT TenantId, Day, FeatureTag, GenAiResponseModel,
           sum(SpanCount) AS SpanCount, sum(EstimatedCost) AS EstimatedCost
    FROM daily_feature_rollup
    GROUP BY TenantId, Day, FeatureTag, GenAiResponseModel
)
WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)
GROUP BY TenantId
ORDER BY span_total DESC
FORMAT PrettyCompact;
