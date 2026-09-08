-- CTO-311: rebuild the rollup targets from the deduplicated raw span table.
-- EXPLICIT, one-shot, operator-run. Not part of `make ch-migrate`. Run `make ch-rollup-rebuild`.
--
-- READ db/clickhouse/checks/rollup_drift.sql AND RUN `make ch-rollup-check` FIRST. This script
-- rewrites three tables the dashboard reads. Do not run it without knowing what it will change.
--
-- WHY A REBUILD AND NOT AN ADJUSTMENT. The three materialized views sum each span into a
-- SummingMergeTree target at INSERT time, so a duplicate is banked before any engine sees it and no
-- merge subtracts it. There is no "remove the duplicate" operation available: the rollup rows do not
-- record which spans they came from. What IS available is the source. otel_spans is a
-- ReplacingMergeTree keyed on span identity (CTO-245), so `FROM otel_spans FINAL` is one row per
-- span, and re-aggregating that at each rollup's grain reproduces exactly what the MV would have
-- written had every span arrived once. The rollups are therefore DERIVABLE, not guessable, and this
-- script derives them. It never computes a correction, a ratio or an estimate.
--
-- ############################################################################################
-- # WHAT IS NOT DERIVABLE, AND IS THEREFORE LEFT COMPLETELY ALONE                             #
-- ############################################################################################
--
-- otel_spans drops raw rows at 90 days (CTO-22/CTO-29; a per-tenant override in storage_tiering.sql
-- can make that shorter). The rollups carry NO TTL on purpose: they are the surviving long-horizon
-- aggregate, and for any period older than raw retention they are the ONLY record that exists.
--
-- Deriving those grains is impossible. There is nothing to derive them from. The tempting move is to
-- write something anyway (scale the surviving days, assume the duplication ratio was uniform, or
-- simply zero them). Every one of those fabricates a dollar figure, which is worse than the problem
-- being fixed: an inflated number an operator can be told about beats a plausible number nobody can
-- audit. So this script CARRIES SUCH GRAINS ACROSS BYTE-FOR-BYTE, including any duplicated money
-- still inside them, and `make ch-rollup-check` reports them separately as `not_derivable` so the
-- residual uncertainty stays visible instead of being silently laundered into a rebuilt table.
--
-- Derivability is judged per (TenantId, Day) and that is exact, not approximate: otel_spans is
-- PARTITION BY toDate(Timestamp) and its TTL DELETE is a function of Timestamp alone, so a day's
-- rows expire together and a day can never be half-present. Every grain of a day that raw still
-- covers is rebuilt; no grain of a day it does not cover is touched. The hourly rollup is judged on
-- toDate(Hour) for the same reason.
--
-- ############################################################################################
-- # BEFORE YOU RUN IT                                                                         #
-- ############################################################################################
--
--   * QUIESCE INGEST. The materialized views keep firing into the CURRENT targets until step 4
--     swaps them, so any span inserted between the derive and the swap is counted into a table that
--     is about to be discarded and is absent from the one that replaces it. Unlike the duplicate
--     this script repairs, that loss is silent. Stop the gateway, or accept a gap and re-post it.
--   * Have disk for a second copy of each rollup. These are aggregates, so this is small next to
--     the CTO-245 raw-table migration, but it is not nothing.
--   * Take a snapshot if this is not a local stack. The pre-rebuild tables survive as
--     <table>_cto311 (step 4 swaps the names), which is your rollback, but only until you drop them.
--   * Run it AFTER any span repricing, not before. Repricing rewrites EstimatedCost on raw spans,
--     and a rollup derived before that still holds the old money. See RUNNING.md, "Order of
--     operations", and the CTO-313 note there.
--
-- Money stays Decimal64(8) USD end to end, the same type on both sides, so every verification below
-- is an exact equality and a failure is a real mismatch, never a rounding artifact.

-- ============================================================================================
-- 1. BUILD BESIDE. Same pattern as db/clickhouse/migrations/otel_spans_replacing_engine.sql:
--    create the shadow table, fill it, verify it, EXCHANGE, keep the old one as the rollback.
--
--    DROP first rather than CREATE IF NOT EXISTS. A shadow left behind by an aborted earlier run
--    would otherwise be topped up by the inserts below and double every figure in it.
-- ============================================================================================
DROP TABLE IF EXISTS daily_feature_rollup_cto311;
DROP TABLE IF EXISTS hourly_feature_rollup_cto311;
DROP TABLE IF EXISTS daily_account_rollup_cto311;

CREATE TABLE daily_feature_rollup_cto311  AS daily_feature_rollup;
CREATE TABLE hourly_feature_rollup_cto311 AS hourly_feature_rollup;
CREATE TABLE daily_account_rollup_cto311  AS daily_account_rollup;

-- `CREATE TABLE ... AS` copies columns, engine, partitioning, sorting key and skipping indexes, but
-- NOT the TTL. That is not a guess: it is the lesson the CTO-245 engine migration paid for, where a
-- missed TTL would have migrated the table cleanly and silently stopped it tiering to warm and cold
-- storage. These three rollups deliberately carry no TTL today (they are the long-horizon aggregate
-- that outlives raw retention), so there is nothing to restore.
--
-- "Deliberately carry no TTL today" is exactly the kind of statement that quietly stops being true,
-- so it is asserted rather than trusted. engine_full contains the whole engine clause, TTL included,
-- so comparing it against the live table catches a TTL, a settings change or a sorting-key change
-- that the copy failed to inherit. If this throws, add the missing clause to the shadow table with
-- ALTER before the exchange; do not skip the guard.
SELECT
    name,
    engine_full,
    throwIf(
        engine_full != (SELECT engine_full FROM system.tables
                        WHERE database = currentDatabase() AND name = 'daily_feature_rollup'),
        'daily_feature_rollup_cto311 did not inherit the live engine clause (TTL is the usual culprit: CREATE TABLE ... AS does not copy it). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FROM system.tables
WHERE database = currentDatabase() AND name = 'daily_feature_rollup_cto311'
FORMAT Vertical;

SELECT
    name,
    engine_full,
    throwIf(
        engine_full != (SELECT engine_full FROM system.tables
                        WHERE database = currentDatabase() AND name = 'hourly_feature_rollup'),
        'hourly_feature_rollup_cto311 did not inherit the live engine clause (TTL is the usual culprit). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FROM system.tables
WHERE database = currentDatabase() AND name = 'hourly_feature_rollup_cto311'
FORMAT Vertical;

SELECT
    name,
    engine_full,
    throwIf(
        engine_full != (SELECT engine_full FROM system.tables
                        WHERE database = currentDatabase() AND name = 'daily_account_rollup'),
        'daily_account_rollup_cto311 did not inherit the live engine clause (TTL is the usual culprit). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FROM system.tables
WHERE database = currentDatabase() AND name = 'daily_account_rollup_cto311'
FORMAT Vertical;

-- ============================================================================================
-- 2. DERIVE the days raw still covers.
--
--    Each SELECT is the corresponding materialized view's SELECT with `FINAL` added and the
--    derivable-day filter applied. They must stay character-for-character equivalent to the MVs in
--    rollups.sql / account_rollups.sql, coverage counters included: a rebuild that computed
--    UnpricedSpanCount differently from the MV would leave the rebuilt history disagreeing with
--    everything ingested after it, on the one column that exists to say how much of the money is
--    actually known.
--
--    FINAL is what makes this a repair rather than a re-run of the bug. Without it the duplicated
--    raw rows that have not merged yet would be re-summed straight back into the new rollup.
-- ============================================================================================
INSERT INTO daily_feature_rollup_cto311
    (TenantId, Day, FeatureTag, GenAiResponseModel,
     InputTokens, OutputTokens, CachedInputTokens, EstimatedCost, ReconciledCost,
     SpanCount, UnknownUsageSpanCount, UnpricedSpanCount, TraceCountState, UserCountState)
SELECT
    TenantId,
    toDate(Timestamp)                                         AS Day,
    FeatureTag,
    GenAiResponseModel,
    ifNull(sum(otel_spans.InputTokens), 0)                    AS InputTokens,
    ifNull(sum(otel_spans.OutputTokens), 0)                   AS OutputTokens,
    ifNull(sum(otel_spans.CachedInputTokens), 0)              AS CachedInputTokens,
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                                   AS SpanCount,
    countIf(
        otel_spans.EstimatedCost IS NULL
        AND multiIf(
            otel_spans.GenAiOperation = 'embeddings', otel_spans.InputTokens IS NULL,
            otel_spans.GenAiOperation IN ('tool', 'vector', 'compute', 'egress'), 0,
            otel_spans.InputTokens IS NULL OR otel_spans.OutputTokens IS NULL
        )
    )                                                         AS UnknownUsageSpanCount,
    countIf(otel_spans.EstimatedCost IS NULL)                 AS UnpricedSpanCount,
    uniqState(TraceId)                                        AS TraceCountState,
    uniqState(UserIdHash)                                     AS UserCountState
FROM otel_spans FINAL
GROUP BY TenantId, Day, FeatureTag, GenAiResponseModel;

INSERT INTO hourly_feature_rollup_cto311
    (TenantId, Hour, FeatureTag, GenAiResponseModel,
     InputTokens, OutputTokens, CachedInputTokens, EstimatedCost, ReconciledCost,
     SpanCount, UnknownUsageSpanCount, UnpricedSpanCount, TraceCountState, UserCountState)
SELECT
    TenantId,
    toStartOfHour(Timestamp)                                  AS Hour,
    FeatureTag,
    GenAiResponseModel,
    ifNull(sum(otel_spans.InputTokens), 0)                    AS InputTokens,
    ifNull(sum(otel_spans.OutputTokens), 0)                   AS OutputTokens,
    ifNull(sum(otel_spans.CachedInputTokens), 0)              AS CachedInputTokens,
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                                   AS SpanCount,
    countIf(
        otel_spans.EstimatedCost IS NULL
        AND multiIf(
            otel_spans.GenAiOperation = 'embeddings', otel_spans.InputTokens IS NULL,
            otel_spans.GenAiOperation IN ('tool', 'vector', 'compute', 'egress'), 0,
            otel_spans.InputTokens IS NULL OR otel_spans.OutputTokens IS NULL
        )
    )                                                         AS UnknownUsageSpanCount,
    countIf(otel_spans.EstimatedCost IS NULL)                 AS UnpricedSpanCount,
    uniqState(TraceId)                                        AS TraceCountState,
    uniqState(UserIdHash)                                     AS UserCountState
FROM otel_spans FINAL
GROUP BY TenantId, Hour, FeatureTag, GenAiResponseModel;

INSERT INTO daily_account_rollup_cto311
    (TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation,
     EstimatedCost, ReconciledCost, SpanCount, UnpricedSpanCount, UserCountState)
SELECT
    TenantId,
    toDate(Timestamp)                                         AS Day,
    AccountIdHash,
    FeatureTag,
    GenAiOperation,
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                                   AS SpanCount,
    countIf(otel_spans.EstimatedCost IS NULL)                 AS UnpricedSpanCount,
    uniqState(UserIdHash)                                     AS UserCountState
FROM otel_spans FINAL
GROUP BY TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation;

-- ============================================================================================
-- 3. CARRY ACROSS the grains raw can no longer speak for, unchanged.
--
--    `SELECT *` on the same table shape, so aggregate states (TraceCountState, UserCountState) move
--    verbatim; re-deriving them is impossible and merging them into anything would be a fabrication.
--    The filter is the exact complement of step 2's coverage, so the two inserts are disjoint and
--    nothing is counted twice. That disjointness matters more here than anywhere else in this
--    script: SummingMergeTree would ADD an overlapping row rather than reject it, and the result
--    would be a fresh, silent over-count of exactly the kind being repaired. Step 4 verifies it.
-- ============================================================================================
INSERT INTO daily_feature_rollup_cto311
SELECT * FROM daily_feature_rollup
WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2);

INSERT INTO hourly_feature_rollup_cto311
SELECT * FROM hourly_feature_rollup
WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2);

INSERT INTO daily_account_rollup_cto311
SELECT * FROM daily_account_rollup
WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2);

-- ============================================================================================
-- 4. VERIFY BEFORE SWAPPING. Every check is a throwIf, so a failure aborts the script with the
--    shadow tables still to one side and the live tables untouched. Nothing here tolerates a
--    near-miss: money is Decimal64(8) on both sides and counts are integers.
--
--    Two assertions per table:
--      a. the derivable half equals a FINAL read of raw spans, exactly. This is the repair.
--      b. the non-derivable half equals what the live table already held, exactly. This is the
--         promise that nothing undeducible was invented, dropped or rescaled.
-- ============================================================================================
SELECT
    'daily_feature_rollup' AS rollup,
    (SELECT sum(SpanCount) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL) AS raw_spans,
    (SELECT sum(EstimatedCost) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL) AS raw_cost,
    (SELECT sum(SpanCount) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans,
    (SELECT sum(SpanCount) FROM daily_feature_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans_before,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'daily_feature_rollup rebuild does not reconcile against a FINAL read of otel_spans. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before,
            'daily_feature_rollup non-derivable grains were not carried across intact. NOT swapping.') AS carried_throws_if_wrong
FORMAT Vertical;

SELECT
    'hourly_feature_rollup' AS rollup,
    (SELECT sum(SpanCount) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL) AS raw_spans,
    (SELECT sum(EstimatedCost) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL) AS raw_cost,
    (SELECT sum(SpanCount) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans,
    (SELECT sum(SpanCount) FROM hourly_feature_rollup
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans_before,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'hourly_feature_rollup rebuild does not reconcile against a FINAL read of otel_spans. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before,
            'hourly_feature_rollup non-derivable grains were not carried across intact. NOT swapping.') AS carried_throws_if_wrong
FORMAT Vertical;

SELECT
    'daily_account_rollup' AS rollup,
    (SELECT sum(SpanCount) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL) AS raw_spans,
    (SELECT sum(EstimatedCost) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL) AS raw_cost,
    (SELECT sum(SpanCount) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans,
    (SELECT sum(SpanCount) FROM daily_account_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) FROM otel_spans GROUP BY 1, 2)) AS carried_spans_before,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'daily_account_rollup rebuild does not reconcile against a FINAL read of otel_spans. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before,
            'daily_account_rollup non-derivable grains were not carried across intact. NOT swapping.') AS carried_throws_if_wrong
FORMAT Vertical;

-- ============================================================================================
-- 5. SWAP. EXCHANGE TABLES is atomic, so there is no window in which a rollup does not exist and no
--    dashboard read sees a half-built table. The materialized views resolve their `TO` target by
--    name, so they keep writing into the live name and land in the rebuilt table from here on
--    (verified against ClickHouse 24.8, same as the CTO-245 exchange).
--
--    After this, <table>_cto311 holds the PRE-REBUILD data. That is the rollback.
-- ============================================================================================
EXCHANGE TABLES daily_feature_rollup  AND daily_feature_rollup_cto311;
EXCHANGE TABLES hourly_feature_rollup AND hourly_feature_rollup_cto311;
EXCHANGE TABLES daily_account_rollup  AND daily_account_rollup_cto311;

-- ============================================================================================
-- 6. WHAT CHANGED. Print it: a rebuild that silently moves a dashboard total is not acceptable even
--    when the new total is the correct one. `recovered_micro_usd` is money the rollups were claiming
--    and raw spans do not support (positive) or money they were missing (negative).
-- ============================================================================================
SELECT
    'daily_feature_rollup' AS rollup,
    (SELECT sum(SpanCount) FROM daily_feature_rollup_cto311) AS spans_before,
    (SELECT sum(SpanCount) FROM daily_feature_rollup)        AS spans_after,
    (SELECT sum(EstimatedCost) FROM daily_feature_rollup_cto311) AS cost_before_usd,
    (SELECT sum(EstimatedCost) FROM daily_feature_rollup)        AS cost_after_usd,
    toInt64(round((cost_before_usd - cost_after_usd) * 1000000)) AS recovered_micro_usd
UNION ALL
SELECT
    'hourly_feature_rollup',
    (SELECT sum(SpanCount) FROM hourly_feature_rollup_cto311),
    (SELECT sum(SpanCount) FROM hourly_feature_rollup),
    (SELECT sum(EstimatedCost) FROM hourly_feature_rollup_cto311),
    (SELECT sum(EstimatedCost) FROM hourly_feature_rollup),
    toInt64(round(((SELECT sum(EstimatedCost) FROM hourly_feature_rollup_cto311)
                 - (SELECT sum(EstimatedCost) FROM hourly_feature_rollup)) * 1000000))
UNION ALL
SELECT
    'daily_account_rollup',
    (SELECT sum(SpanCount) FROM daily_account_rollup_cto311),
    (SELECT sum(SpanCount) FROM daily_account_rollup),
    (SELECT sum(EstimatedCost) FROM daily_account_rollup_cto311),
    (SELECT sum(EstimatedCost) FROM daily_account_rollup),
    toInt64(round(((SELECT sum(EstimatedCost) FROM daily_account_rollup_cto311)
                 - (SELECT sum(EstimatedCost) FROM daily_account_rollup)) * 1000000))
FORMAT PrettyCompact;

-- ============================================================================================
-- 7. THEN, AND ONLY THEN, DROP THE ROLLBACK COPIES. Re-run `make ch-rollup-check` first: the
--    derivable class must come back with drift_micro_usd = 0, and the not_derivable class must
--    report the same grains and money it reported before the rebuild. Keep the copies until the
--    dashboard has been looked at.
--
--   DROP TABLE daily_feature_rollup_cto311;
--   DROP TABLE hourly_feature_rollup_cto311;
--   DROP TABLE daily_account_rollup_cto311;
--
-- TO ROLL BACK instead, exchange them back:
--
--   EXCHANGE TABLES daily_feature_rollup  AND daily_feature_rollup_cto311;
--   EXCHANGE TABLES hourly_feature_rollup AND hourly_feature_rollup_cto311;
--   EXCHANGE TABLES daily_account_rollup  AND daily_account_rollup_cto311;
-- ============================================================================================
