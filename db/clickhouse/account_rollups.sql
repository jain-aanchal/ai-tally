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
ORDER BY (TenantId, AccountIdHash, Day, FeatureTag, GenAiOperation);

CREATE MATERIALIZED VIEW IF NOT EXISTS daily_account_rollup_mv
TO daily_account_rollup
AS SELECT
    TenantId,
    toDate(Timestamp)                      AS Day,
    AccountIdHash,
    FeatureTag,
    GenAiOperation,
    sum(EstimatedCost)                     AS EstimatedCost,
    -- ReconciledCost is Nullable on otel_spans and NOT NULL here. ifNull(..., 0) is what makes the
    -- SummingMergeTree column safe to sum: a NULL would poison the total. A day that has not been
    -- reconciled therefore reads as 0 reconciled, and callers compare against EstimatedCost rather
    -- than treating 0 as "this cost nothing".
    sum(ifNull(ReconciledCost, toDecimal64(0, 8))) AS ReconciledCost,
    count()                                AS SpanCount,
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
--          sum(EstimatedCost)                             AS EstimatedCost,
--          sum(ifNull(ReconciledCost, toDecimal64(0, 8))) AS ReconciledCost,
--          count()                                        AS SpanCount,
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
