-- Account rollup materialized view (ai-tally telemetry store)
-- Implements CTO-183 (B4). Builds on CTO-180, which added otel_spans.AccountIdHash.
--
-- WHY THIS TABLE EXISTS. The per-customer cost tab answers "what does each of my customers cost
-- me?". Without a rollup that question is a scan of otel_spans grouped by AccountIdHash, and
-- AccountIdHash is nowhere near the front of that table's ORDER BY
-- (TenantId, FeatureTag, ServiceName, SpanName, Timestamp) — so ClickHouse cannot skip a single
-- granule on the account dimension and the query degrades into a full tenant scan. That is
-- survivable for a tenant with a dozen accounts and is not survivable for a tenant with thousands,
-- which is exactly the shape of customer this tab is built for.
--
-- WHAT IT MAKES FAST. ORDER BY leads with (TenantId, AccountIdHash, Day), so the two reads the tab
-- actually issues both become index range scans instead of scans:
--   1. rank every account for a tenant over a window   -> one contiguous range per tenant
--   2. drill into ONE account's cost over time         -> one contiguous range per account
-- It also outlives raw retention. otel_spans drops raw rows at 90d (CTO-22/CTO-29); this rollup is
-- an independent table with no such TTL, so per-customer history keeps working after the spans
-- behind it are gone. Same reasoning as daily_feature_rollup in rollups.sql.
--
-- WHY IT IS SEPARATE FROM daily_feature_rollup. That table's key is
-- (TenantId, FeatureTag, GenAiResponseModel, Day) with no account dimension at all, so it cannot
-- answer a per-customer question. Adding AccountIdHash to it instead would multiply every existing
-- row by the account cardinality and slow down the dashboard queries that read it today. A second
-- narrow table is cheaper than widening the hot one.
--
-- WHY GenAiOperation IS CARRIED. It is the input to the six-layer cost split (llm, tools,
-- embeddings, vector, compute, egress) — see LAYER_CASE in web/lib/clickhouse.ts, which maps
-- operation to layer with a multiIf. Keeping the raw operation here rather than a pre-computed
-- layer string means the mapping stays owned by one place in the app and can be corrected without
-- rebuilding this table. It is also what lets a later change separate DIRECT customer cost from
-- ALLOCATED shared cost (allocation keys off the operation) without a second table.
--
-- Columns follow daily_feature_rollup exactly: Decimal64(8) for money (never Float64), and
-- UserCountState as an AggregateFunction state. Query the state with the -Merge combinator:
--   SELECT uniqMerge(UserCountState) FROM daily_account_rollup WHERE ...
-- Summing UserCountState or reading it raw is meaningless.
--
-- AccountIdHash = '' is the UNATTRIBUTED bucket, not a customer. Callers must render it as such
-- and must never rank it alongside real accounts. See the CTO-180 comment in otel_spans.sql.

CREATE TABLE IF NOT EXISTS daily_account_rollup
(
    TenantId            LowCardinality(String),
    Day                 Date,
    AccountIdHash       FixedString(64),
    FeatureTag          LowCardinality(String),
    GenAiOperation      LowCardinality(String),
    EstimatedCost       Decimal64(8),
    ReconciledCost      Decimal64(8),
    SpanCount           UInt64,
    -- CTO-244. otel_spans.EstimatedCost is Nullable: a call we could not price is unpriced, not
    -- free. sum() skips those rows, so EstimatedCost above is a LOWER BOUND on what this account
    -- really cost. This counter is what keeps that from being silent. A caller that finds it
    -- non-zero must label the figure partial rather than rank an under-count as a total.
    UnpricedSpanCount   UInt64,
    UserCountState      AggregateFunction(uniq, FixedString(64))
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(Day)
-- The (TenantId, AccountIdHash, Day) PREFIX is the access path the per-customer tab needs, and it
-- leads deliberately: TenantId first is load-bearing on a shared cluster (CTO-18), AccountIdHash
-- second is what turns "rank thousands of accounts" into a range scan, Day third bounds the window.
--
-- FeatureTag and GenAiOperation are appended to the key rather than left out of it, and that is
-- required for correctness, not a preference. SummingMergeTree collapses rows that share the
-- FULL sorting key and gives every non-key, non-aggregate column an ARBITRARY value from the
-- collapsed set. Were these two columns outside the key, a background merge would quietly fold a
-- day's rows for an account together and stamp whichever FeatureTag and GenAiOperation it happened
-- to see last onto the summed total — silently destroying the per-layer split this table carries
-- them for. Appending them keeps the prefix, and therefore the fast path, exactly as intended
-- while making the grain of a row honest: one row per account per day per feature per operation.
ORDER BY (TenantId, AccountIdHash, Day, FeatureTag, GenAiOperation)
-- Retention (CTO-338): BOOK OF RECORD, 7 years, same standing as daily_feature_rollup and for the
-- same reason. The comment above says this table "outlives raw retention ... with no such TTL";
-- that was true and it was also unbounded growth. It now outlives raw retention by a stated 7
-- years instead of by omission. Margin per customer is not answerable for a period whose account
-- rollup is gone, and nothing else in the database can reconstruct it once the spans are dropped.
TTL toDateTime(Day) + INTERVAL 2555 DAY DELETE;

-- CTO-244 migration for an EXISTING deployment. The CREATE TABLE above is IF NOT EXISTS and so is
-- a no-op on a live stack; add the coverage counter explicitly. `AFTER SpanCount` keeps the
-- physical column order identical to the CREATE TABLE. DEFAULT 0 makes it metadata-only, and on
-- pre-cutover rows that 0 means "nobody counted", NOT "there were no unknowns": before the cutover
-- an unpriced call was indistinguishable from a free one. Those figures may understate spend and
-- cannot be corrected after the fact. See the CTO-244 note in otel_spans.sql and RUNNING.md.
--
-- The MV is DROPped and recreated rather than CREATE ... IF NOT EXISTS, because a materialized
-- view's SELECT cannot be ALTERed and IF NOT EXISTS would silently leave the old definition in
-- place. Dropping a `TO`-table MV leaves the target table and all its history untouched.
ALTER TABLE daily_account_rollup
    ADD COLUMN IF NOT EXISTS UnpricedSpanCount UInt64 DEFAULT 0 AFTER SpanCount;

DROP VIEW IF EXISTS daily_account_rollup_mv;
CREATE MATERIALIZED VIEW daily_account_rollup_mv
TO daily_account_rollup
AS SELECT
    TenantId,
    toDate(Timestamp)                      AS Day,
    AccountIdHash,
    FeatureTag,
    GenAiOperation,
    -- CTO-244: ifNull sits OUTSIDE the aggregate (an all-NULL group sums to NULL) so the result
    -- stays non-Nullable for the SummingMergeTree column. The honesty lives in UnpricedSpanCount
    -- below, not in this sum.
    ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8)) AS EstimatedCost,
    -- ReconciledCost is Nullable on otel_spans and NOT NULL here. ifNull(..., 0) is what makes the
    -- SummingMergeTree column safe to sum: a NULL would poison the total. A day that has not been
    -- reconciled therefore reads as 0 reconciled, and callers compare against EstimatedCost rather
    -- than treating 0 as "this cost nothing".
    ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
    count()                                AS SpanCount,
    countIf(otel_spans.EstimatedCost IS NULL) AS UnpricedSpanCount,
    uniqState(UserIdHash)                  AS UserCountState
FROM otel_spans
GROUP BY TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation;

-- APPLYING THIS TO AN EXISTING DEPLOYMENT, AND BACKFILLING.
--
-- Two separate problems, and skipping either one leaves the tab reading an empty table.
--
-- 1. GETTING THE DDL IN. compose mounts db/clickhouse into /docker-entrypoint-initdb.d, which runs
--    ONLY on a first boot against an empty volume. A stack that is already up will never execute
--    the statements above on its own. Replay the canonical DDL with `make ch-migrate` from infra/
--    (this file is in that target's CH_DDL list and in the compose mount list). Every statement
--    here is CREATE ... IF NOT EXISTS, so replaying is idempotent and safe against a populated
--    database. This is not hypothetical: the Postgres side of this repo has already shipped
--    migrations (0011, 0012, 0015, 0016) that silently never reached an existing volume this way.
--
-- 2. GETTING THE EXISTING DATA IN. A ClickHouse materialized view is an INSERT trigger, not a
--    view over history. daily_account_rollup_mv captures rows inserted AFTER it is created and
--    nothing before, so on any database that already holds spans the table starts empty and then
--    silently begins mid-history. Backfill explicitly, once, right after creating the MV:
--
--      INSERT INTO daily_account_rollup
--      SELECT
--          TenantId,
--          toDate(Timestamp)                              AS Day,
--          AccountIdHash,
--          FeatureTag,
--          GenAiOperation,
--          ifNull(sum(otel_spans.EstimatedCost), toDecimal64(0, 8))  AS EstimatedCost,
--          ifNull(sum(otel_spans.ReconciledCost), toDecimal64(0, 8)) AS ReconciledCost,
--          count()                                        AS SpanCount,
--          countIf(EstimatedCost IS NULL)                 AS UnpricedSpanCount,
--          uniqState(UserIdHash)                          AS UserCountState
--      FROM otel_spans
--      WHERE Timestamp < '<cutoff>'          -- see the double-count warning below
--      GROUP BY TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation;
--
--    DOUBLE COUNTING IS THE ONLY REAL HAZARD. The MV is already live by the time the backfill runs,
--    so any span inserted after MV creation is counted by the MV AND would be counted again by an
--    unbounded backfill. SummingMergeTree adds those rows together rather than rejecting them, so
--    the damage is silent inflated cost. Pick a `<cutoff>` at or before the MV creation time and
--    backfill strictly below it. If a backfill is ever run wrong, do not try to subtract: TRUNCATE
--    the table and redo both steps, because the source of truth is otel_spans and rebuilding is
--    cheap for as long as the raw rows are still inside the 90d retention window.
--
--    Backfill month by month (`AND toYYYYMM(Timestamp) = 202601`) on a large table to keep peak
--    memory bounded — the GROUP BY is over the whole scanned range otherwise.
--
--    VERIFY after backfilling. The rollup must reconcile exactly against the raw table:
--
--      SELECT sum(EstimatedCost), sum(SpanCount) FROM daily_account_rollup
--       WHERE TenantId = 'local-dev' AND Day >= today() - 29;
--      SELECT sum(EstimatedCost), count()        FROM otel_spans
--       WHERE TenantId = 'local-dev' AND toDate(Timestamp) >= today() - 29;
--
--    Costs are Decimal64(8) on both sides, so this is an exact equality, not an approximate one.
--    A mismatch means a double-counted or missed window, not a rounding artifact.
