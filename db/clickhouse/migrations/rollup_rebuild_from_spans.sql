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
-- can make that shorter). The rollups outlive it by a wide margin, deliberately: they are the
-- surviving long-horizon aggregate, and for any period older than raw retention they are the ONLY
-- record that exists. CTO-338 gave them a stated horizon instead of an unbounded one (7 years for
-- the daily and account rollups, 13 months for the hourly resolution tier), which does not change
-- anything below: everything this script calls not_derivable is a grain whose RAW spans have gone,
-- and every rollup horizon is far longer than the raw one.
--
-- Deriving those grains is impossible. There is nothing to derive them from. The tempting move is to
-- write something anyway (scale the surviving days, assume the duplication ratio was uniform, or
-- simply zero them). Every one of those fabricates a dollar figure, which is worse than the problem
-- being fixed: an inflated number an operator can be told about beats a plausible number nobody can
-- audit. So this script CARRIES SUCH GRAINS ACROSS BYTE-FOR-BYTE, including any duplicated money
-- still inside them, and `make ch-rollup-check` reports them separately as `not_derivable` so the
-- residual uncertainty stays visible instead of being silently laundered into a rebuilt table.
--
-- ############################################################################################
-- # HOW DERIVABILITY IS JUDGED, AND WHY "RAW HAS A ROW FOR THIS DAY" IS NOT ENOUGH            #
-- ############################################################################################
--
-- An earlier revision of this script judged a (TenantId, Day) derivable whenever raw held at least
-- one row for it, on the argument that otel_spans is PARTITION BY toDate(Timestamp) so a day expires
-- as a unit and can never be half-present. THAT ARGUMENT IS WRONG, and it was wrong in the direction
-- that silently destroys money, so it is written out here rather than merely corrected.
--
-- The TTL DELETE clause is a PER-ROW expression (`toDateTime(Timestamp) + INTERVAL 90 DAY DELETE`,
-- and storage_tiering.sql's per-tenant override compiles to a per-row multiIf on top of that).
-- ClickHouse only drops a whole partition on expiry when `ttl_only_drop_parts = 1` AND the whole
-- part is expired; with the default `ttl_only_drop_parts = 0` it expires INDIVIDUAL ROWS during
-- merges. So on the one day D that straddles the retention boundary, D's morning can already be
-- deleted while D's afternoon survives.
--
-- Under the old predicate that was a silent, mechanical loss of real money:
--   * step 3 saw D in the raw day set, so it did NOT carry D's rollup rows;
--   * step 2 rebuilt every grain of D from the surviving afternoon alone;
--   * the morning's spend was deleted from the rollup, which is the only record of it once raw
--     ages out;
--   * and step 4 could not catch it, because both sides of its reconciliation were computed from
--     the same already-truncated `otel_spans FINAL` read. It reconciled perfectly while the money
--     vanished.
--
-- So derivability is judged against the RETENTION FLOOR, not against "raw has a row here":
--
--   a (TenantId, Day) is DERIVABLE  <=>  raw still holds rows for it
--                                        AND that day is not at or within one day of the moment its
--                                        rows become eligible for TTL deletion.
--
-- The floor is not hardcoded and not parsed out of the DDL. It is read from
-- system.parts.delete_ttl_info_min, which is ClickHouse's own evaluation of the DELETE TTL
-- expression over the rows of each part. That is exact for the default policy AND for any
-- per-tenant multiIf override, because the server computed it with the real expression rather than
-- with an assumption made here. Since PARTITION BY toDate(Timestamp) makes one partition one day,
-- a day whose earliest expiry moment has arrived (or arrives within a day) is treated as possibly
-- truncated and is CARRIED instead of rebuilt.
--
-- Two deliberate conservatisms, both erring towards carrying rather than rebuilding:
--   * partitions are per-day, not per (tenant, day), so a day at risk for the tenant with the
--     shortest retention is carried for every tenant on that day. Carrying leaves acknowledged,
--     reported uncertainty in place; rebuilding a truncated day destroys money. Those are not
--     symmetric, so this errs the safe way.
--   * a day is excluded for one day BEFORE its frontier as well as after, so a run that straddles
--     midnight, or a merge that lands mid-run, cannot move a day from one side to the other while
--     the script is executing.
--
-- ############################################################################################
-- # BEFORE YOU RUN IT                                                                         #
-- ############################################################################################
--
--   * QUIESCE INGEST. The materialized views keep firing into the CURRENT targets until step 5
--     swaps them, so any span inserted between the derive and the swap is counted into a table that
--     is about to be discarded and is absent from the one that replaces it. Unlike the duplicate
--     this script repairs, that loss is silent. Stop the gateway, or accept a gap and re-post it.
--   * Have disk for a second copy of each rollup. These are aggregates, so this is small next to
--     the CTO-245 raw-table migration, but it is not nothing.
--   * PEAK MEMORY IS NOT BOUNDED BY THIS SCRIPT. Step 2's three inserts are whole-table
--     `FROM otel_spans FINAL` reads with a GROUP BY, with no time window and no chunking, which is
--     the opposite of the month-by-month backfill account_rollups.sql prescribes for the same
--     shape. On a large raw table expect to raise max_memory_usage / max_bytes_before_external_
--     group_by, or to run the three inserts by hand one month at a time (the predicates are
--     additive, so `AND toDate(Timestamp) BETWEEN ... AND ...` on each is safe as long as the
--     months tile the derivable range exactly once). A failure here is safe: it aborts long before
--     the EXCHANGE and the live tables are untouched. It does leave half-filled shadow tables
--     behind, which is what step 0's guard and step 1's DROP exist to deal with.
--   * Take a snapshot if this is not a local stack. The pre-rebuild tables survive as
--     <table>_cto311 (step 5 swaps the names), which is your rollback, but only until you drop them.
--   * Run it AFTER any span repricing, not before. Repricing rewrites EstimatedCost on raw spans,
--     and a rollup derived before that still holds the old money. See RUNNING.md, "Order of
--     operations", and the CTO-313 note there.
--
-- Money stays Decimal64(8) USD end to end, the same type on both sides, so every verification below
-- is an exact equality and a failure is a real mismatch, never a rounding artifact.

-- ============================================================================================
-- 0. PREFLIGHT. Two guards, and both of them are about not destroying something irreplaceable.
--
--    Note the shape: every throwIf below sits in the select list of a query with NO FROM clause, or
--    reads its inputs through scalar subqueries. That is deliberate. A throwIf placed in the select
--    list of a query filtered to one table name is not evaluated at all when that name is absent,
--    because zero matching rows means zero evaluations, and the query then SUCCEEDS. A guard that
--    passes precisely when the thing it guards is missing is not a guard.
-- ============================================================================================

-- 0a. A pre-existing, non-empty rollback copy means an earlier run already completed and these
--     tables ARE the pre-rebuild snapshot. Step 1 would DROP them. For the `not_derivable` grains
--     that snapshot is, by definition, the only surviving record of that money: raw cannot rebuild
--     them and nothing else holds them. So refuse, and make the operator decide explicitly.
SELECT
    (SELECT ifNull(sum(rows), 0) FROM system.parts
      WHERE database = currentDatabase() AND active
        AND table IN ('daily_feature_rollup_cto311',
                      'hourly_feature_rollup_cto311',
                      'daily_account_rollup_cto311')) AS existing_rollback_rows,
    throwIf(
        existing_rollback_rows > 0,
        'A non-empty *_cto311 rollback copy already exists, which means a previous rebuild completed and those tables are the pre-rebuild snapshot. Running again would DROP them, and for not_derivable grains that snapshot is the only record of that money. Verify the dashboard, then DROP the *_cto311 tables by hand before re-running, or EXCHANGE them back to roll the previous run back.'
    ) AS rollback_copy_throws_if_present
FORMAT Vertical;

-- 0b. The same ReplacingMergeTree preflight the read-only check refuses to run without, for the
--     same reason and with more at stake. Every "truth" figure below is a FINAL read of otel_spans,
--     which only deduplicates if the table is a ReplacingMergeTree whose sorting key carries span
--     identity. On a plain MergeTree, FINAL is a no-op: this script would re-derive the rollups
--     INCLUDING every duplicate, verify them against an equally inflated raw read, pass, swap, and
--     print a "recovered" figure. Run `make ch-migrate-otel-engine` first (CTO-245).
SELECT
    (SELECT engine      FROM system.tables
      WHERE database = currentDatabase() AND name = 'otel_spans') AS engine,
    (SELECT sorting_key FROM system.tables
      WHERE database = currentDatabase() AND name = 'otel_spans') AS sorting_key,
    throwIf(
        engine != 'ReplacingMergeTree' OR NOT endsWith(sorting_key, 'TraceId, SpanId'),
        'otel_spans is not a ReplacingMergeTree keyed on span identity, so FINAL does not dedupe and this rebuild would re-derive the rollups from duplicated rows and verify them against an equally duplicated truth. Run make ch-migrate-otel-engine first (CTO-245).'
    ) AS preflight_throws_if_broken
FORMAT Vertical;

-- 0c. The retention floor is read from system.parts.delete_ttl_info_min (see the header). A part
--     carries no delete TTL info when the table has no DELETE TTL at all, which is fine, and also
--     when a TTL was added by ALTER and the part has not been re-merged or MATERIALIZEd since,
--     which is not: those rows are subject to expiry that this script cannot see. A MIXTURE of the
--     two states across active parts is exactly that second case, so refuse.
SELECT
    (SELECT countIf(delete_ttl_info_min  = toDateTime(0)) FROM system.parts
      WHERE database = currentDatabase() AND table = 'otel_spans' AND active) AS parts_without_ttl_info,
    (SELECT countIf(delete_ttl_info_min != toDateTime(0)) FROM system.parts
      WHERE database = currentDatabase() AND table = 'otel_spans' AND active) AS parts_with_ttl_info,
    throwIf(
        parts_without_ttl_info > 0 AND parts_with_ttl_info > 0,
        'otel_spans has active parts with no DELETE TTL info alongside parts that have it, so a MODIFY TTL has not been applied to every part and the retention floor this script reads is not the one the server will enforce. Run ALTER TABLE otel_spans MATERIALIZE TTL first, then re-run.'
    ) AS ttl_info_throws_if_stale
FORMAT Vertical;

-- ============================================================================================
-- 1. BUILD BESIDE. Same pattern as db/clickhouse/migrations/otel_spans_replacing_engine.sql:
--    create the shadow table, fill it, verify it, EXCHANGE, keep the old one as the rollback.
--
--    DROP first rather than CREATE IF NOT EXISTS. A shadow left behind by an aborted earlier run
--    would otherwise be topped up by the inserts below and double every figure in it. Step 0a has
--    already established that no COMPLETED run's snapshot is being destroyed here.
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
-- storage.
--
-- CTO-338 is the moment that lesson stopped being hypothetical here. These three rollups used to
-- carry no TTL, and the guard below was written to catch the day that changed. They now carry the
-- retention policy generated from tally.storage_tiering, so the shadow tables come out of the
-- CREATEs above with NO TTL while the live tables have one, and exchanging them would silently
-- return all three rollups to unbounded growth. Restore it explicitly, before the guard runs.
--
-- These restatements must stay identical to the TTL clauses in rollups.sql and account_rollups.sql.
-- The guard is what enforces that: it compares the whole engine clause, so a horizon changed in one
-- place and not the other throws here rather than being exchanged into production.
--
-- If a per-tenant override is in force (a multiIf DELETE expression rather than the flat interval
-- below), these three statements must carry the SAME multiIf. Regenerate them with
-- tally.storage_tiering.retention_for(<table>).render_alter(<overrides>) rather than editing by
-- hand, then re-run.
SET materialize_ttl_after_modify = 0;
ALTER TABLE daily_feature_rollup_cto311  MODIFY TTL toDateTime(Day) + INTERVAL 2555 DAY DELETE;
ALTER TABLE hourly_feature_rollup_cto311 MODIFY TTL toDateTime(Hour) + INTERVAL 400 DAY DELETE;
ALTER TABLE daily_account_rollup_cto311  MODIFY TTL toDateTime(Day) + INTERVAL 2555 DAY DELETE;

-- The shadow tables are empty at this point, so materialize_ttl_after_modify is moot for them; it
-- is set anyway so that nothing in this script can trigger a mass expiry as a side effect.
--
-- The guard below is unchanged in intent: engine_full contains the whole engine clause, TTL
-- included, so comparing it against the live table catches a TTL, a settings change or a
-- sorting-key change that the copy failed to inherit. If this throws, add the missing clause to the
-- shadow table with ALTER before the exchange; do not skip the guard.
--
-- Both sides are read as scalar subqueries so the throwIf is evaluated exactly once whether or not
-- the shadow table exists. An absent shadow yields the String default '', which fails the equality
-- and throws, which is the correct answer to "the table you were about to fill is not there".
SELECT
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'daily_feature_rollup_cto311') AS shadow_engine,
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'daily_feature_rollup')         AS live_engine,
    throwIf(
        shadow_engine = '' OR shadow_engine != live_engine,
        'daily_feature_rollup_cto311 is missing or did not inherit the live engine clause (TTL is the usual culprit: CREATE TABLE ... AS does not copy it). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FORMAT Vertical;

SELECT
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'hourly_feature_rollup_cto311') AS shadow_engine,
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'hourly_feature_rollup')         AS live_engine,
    throwIf(
        shadow_engine = '' OR shadow_engine != live_engine,
        'hourly_feature_rollup_cto311 is missing or did not inherit the live engine clause (TTL is the usual culprit). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FORMAT Vertical;

SELECT
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'daily_account_rollup_cto311') AS shadow_engine,
    (SELECT engine_full FROM system.tables
      WHERE database = currentDatabase() AND name = 'daily_account_rollup')         AS live_engine,
    throwIf(
        shadow_engine = '' OR shadow_engine != live_engine,
        'daily_account_rollup_cto311 is missing or did not inherit the live engine clause (TTL is the usual culprit). Fix the shadow table before exchanging.'
    ) AS engine_clause_throws_if_lost
FORMAT Vertical;

-- ============================================================================================
-- 2. DERIVE the days raw can still speak for in full.
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
--
--    THE `WHERE ... IN` FILTER IS LOAD-BEARING AND MUST STAY THE EXACT COMPLEMENT OF STEP 3'S.
--    These targets are SummingMergeTree: a grain written by both steps is ADDED, not rejected, and
--    the result is a fresh silent over-count of exactly the kind being repaired. The two filters
--    are literal negations of one another over the same subquery text, and step 4 asserts the
--    partition arithmetically rather than trusting that they still are.
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
WHERE (TenantId, toDate(Timestamp)) IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
)
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
WHERE (TenantId, toDate(Timestamp)) IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
)
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
WHERE (TenantId, toDate(Timestamp)) IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
)
GROUP BY TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation;

-- ============================================================================================
-- 3. CARRY ACROSS the grains raw can no longer speak for in full, unchanged.
--
--    `SELECT *` on the same table shape, so aggregate states (TraceCountState, UserCountState) move
--    verbatim; re-deriving them is impossible and merging them into anything would be a fabrication.
--    The filter is the literal NOT IN of step 2's IN, over the same subquery text, so the two
--    inserts are disjoint AND exhaustive. That matters more here than anywhere else in this script:
--    SummingMergeTree would ADD an overlapping row rather than reject it, and a gap would delete
--    money that exists nowhere else. Step 4 asserts both properties against the SOURCES rather than
--    inspecting the filters.
-- ============================================================================================
INSERT INTO daily_feature_rollup_cto311
SELECT * FROM daily_feature_rollup
WHERE (TenantId, Day) NOT IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
);

INSERT INTO hourly_feature_rollup_cto311
SELECT * FROM hourly_feature_rollup
WHERE (TenantId, toDate(Hour)) NOT IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
);

INSERT INTO daily_account_rollup_cto311
SELECT * FROM daily_account_rollup
WHERE (TenantId, Day) NOT IN (
    SELECT TenantId, toDate(Timestamp) AS Day
    FROM otel_spans
    WHERE toDate(Timestamp) NOT IN (
        SELECT toDate(partition) FROM system.parts
        WHERE database = currentDatabase() AND table = 'otel_spans' AND active
          AND delete_ttl_info_min != toDateTime(0)
          AND delete_ttl_info_min <= now() + INTERVAL 1 DAY
    )
    GROUP BY 1, 2
);

-- ============================================================================================
-- 4. VERIFY BEFORE SWAPPING. Every check is a throwIf, so a failure aborts the script with the
--    shadow tables still to one side and the live tables untouched. Nothing here tolerates a
--    near-miss: money is Decimal64(8) on both sides and counts are integers.
--
--    Three assertions per table:
--      a. the derivable half equals a FINAL read of raw spans over the derivable days, exactly.
--         This is the repair.
--      b. the non-derivable half equals what the live table already held: SpanCount, EstimatedCost
--         AND ReconciledCost. This is the promise that nothing undeducible was invented, dropped or
--         rescaled, and it is asserted on the money and not only on the row count, because it is
--         the money that cannot be reconstructed if it is wrong.
--      c. the two halves add up to the whole shadow table. (a) and (b) each compare one half of the
--         shadow against its own source; (c) compares the shadow TOTAL against the sum of the two
--         sources, so a grain written by BOTH inserts, or by neither, fails here even though it may
--         sit inside whichever half still balances. This is the structural statement that step 2's
--         and step 3's filters partition the space, rather than a comment claiming they do.
-- ============================================================================================
SELECT
    'daily_feature_rollup' AS rollup,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_spans,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_cost,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_feature_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans_before,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_feature_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost_before,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM daily_feature_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM daily_feature_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled_before,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_feature_rollup_cto311) AS shadow_spans_total,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'daily_feature_rollup rebuild does not reconcile against a FINAL read of otel_spans over the derivable days. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before
            OR carried_cost != carried_cost_before
            OR carried_reconciled != carried_reconciled_before,
            'daily_feature_rollup non-derivable grains were not carried across intact (spans, estimated cost or reconciled cost moved). NOT swapping.') AS carried_throws_if_wrong,
    throwIf(raw_spans + carried_spans_before != shadow_spans_total,
            'daily_feature_rollup shadow total does not equal derivable raw spans plus carried rollup spans, so the derive and carry filters are not a partition: a grain was written twice (SummingMergeTree ADDS it) or dropped. NOT swapping.') AS partition_throws_if_overlapping
FORMAT Vertical;

SELECT
    'hourly_feature_rollup' AS rollup,
    (SELECT ifNull(sum(SpanCount), 0) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_spans,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_cost,
    (SELECT ifNull(sum(SpanCount), 0) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans,
    (SELECT ifNull(sum(SpanCount), 0) FROM hourly_feature_rollup
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans_before,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM hourly_feature_rollup
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost_before,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM hourly_feature_rollup_cto311
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM hourly_feature_rollup
      WHERE (TenantId, toDate(Hour)) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled_before,
    (SELECT ifNull(sum(SpanCount), 0) FROM hourly_feature_rollup_cto311) AS shadow_spans_total,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'hourly_feature_rollup rebuild does not reconcile against a FINAL read of otel_spans over the derivable days. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before
            OR carried_cost != carried_cost_before
            OR carried_reconciled != carried_reconciled_before,
            'hourly_feature_rollup non-derivable grains were not carried across intact (spans, estimated cost or reconciled cost moved). NOT swapping.') AS carried_throws_if_wrong,
    throwIf(raw_spans + carried_spans_before != shadow_spans_total,
            'hourly_feature_rollup shadow total does not equal derivable raw spans plus carried rollup spans, so the derive and carry filters are not a partition. NOT swapping.') AS partition_throws_if_overlapping
FORMAT Vertical;

SELECT
    'daily_account_rollup' AS rollup,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_spans,
    (SELECT count() FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_spans,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS rebuilt_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM otel_spans FINAL
      WHERE (TenantId, toDate(Timestamp)) IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS raw_cost,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_account_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_spans_before,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost,
    (SELECT ifNull(sum(EstimatedCost), toDecimal64(0, 8)) FROM daily_account_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_cost_before,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM daily_account_rollup_cto311
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled,
    (SELECT ifNull(sum(ReconciledCost), toDecimal64(0, 8)) FROM daily_account_rollup
      WHERE (TenantId, Day) NOT IN (SELECT TenantId, toDate(Timestamp) AS Day FROM otel_spans
             WHERE toDate(Timestamp) NOT IN (SELECT toDate(partition) FROM system.parts
                    WHERE database = currentDatabase() AND table = 'otel_spans' AND active
                      AND delete_ttl_info_min != toDateTime(0)
                      AND delete_ttl_info_min <= now() + INTERVAL 1 DAY) GROUP BY 1, 2)) AS carried_reconciled_before,
    (SELECT ifNull(sum(SpanCount), 0) FROM daily_account_rollup_cto311) AS shadow_spans_total,
    throwIf(rebuilt_spans != raw_spans OR rebuilt_cost != raw_cost,
            'daily_account_rollup rebuild does not reconcile against a FINAL read of otel_spans over the derivable days. NOT swapping.') AS derivable_throws_if_wrong,
    throwIf(carried_spans != carried_spans_before
            OR carried_cost != carried_cost_before
            OR carried_reconciled != carried_reconciled_before,
            'daily_account_rollup non-derivable grains were not carried across intact (spans, estimated cost or reconciled cost moved). NOT swapping.') AS carried_throws_if_wrong,
    throwIf(raw_spans + carried_spans_before != shadow_spans_total,
            'daily_account_rollup shadow total does not equal derivable raw spans plus carried rollup spans, so the derive and carry filters are not a partition. NOT swapping.') AS partition_throws_if_overlapping
FORMAT Vertical;

-- ============================================================================================
-- 5. SWAP. EXCHANGE TABLES is atomic, so there is no window in which a rollup does not exist and no
--    dashboard read sees a half-built table. The materialized views resolve their `TO` target by
--    name, so they keep writing into the live name and land in the rebuilt table from here on
--    (verified against ClickHouse 24.8, same as the CTO-245 exchange).
--
--    EACH EXCHANGE IS ATOMIC; THE THREE TOGETHER ARE NOT. Between the first and the third there is
--    a window, short but real, in which the dashboard reads a rebuilt daily rollup beside a still
--    inflated hourly one and the two disagree by whatever the drift was. Quiescing ingest stops
--    writes, not reads, so it does not close this window. If a reader seeing an inconsistent pair
--    matters, take the dashboard down for the duration; otherwise expect it and do not chase it.
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
--    Until they are dropped, step 0a refuses to run this script again, because for the
--    not_derivable grains those copies are the only surviving record of that money.
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
