-- Apply the CTO-338 derived-table retention policy to an EXISTING, POPULATED install.
--
-- ############################################################################################
-- # READ THIS BEFORE RUNNING IT. THIS SCRIPT IS DELIBERATELY NOT PART OF `make ch-migrate`.   #
-- ############################################################################################
--
-- Everything in db/clickhouse is CREATE ... IF NOT EXISTS or ALTER ... ADD COLUMN IF NOT EXISTS,
-- which is what makes `make ch-migrate` cheap, metadata-only and safe to replay against a live
-- database. A TTL is the first thing in this repo that is none of those. The TTL clauses added to
-- the CREATE TABLE statements alongside this file are free, because they only take effect on a
-- FRESH database where there is nothing to expire. Adding the same TTL to a table that already
-- holds a year of rows is a different operation entirely, and this is where it lives, next to
-- ch-migrate-otel-engine and rollup_rebuild_from_spans.sql for the same reason those are separate.
--
-- WHAT `ALTER TABLE ... MODIFY TTL` ACTUALLY DOES
--
-- ClickHouse's `materialize_ttl_after_modify` setting DEFAULTS TO 1. On the default, MODIFY TTL
-- does not just record the new rule: it immediately launches a mutation that materializes the TTL
-- across every existing part. On a populated table that is a MASS DELETE of everything already past
-- the horizon, running as fast as the merge scheduler allows, with no confirmation and no dry run.
-- On the tables here that is potentially the majority of the table on the first statement.
--
-- So step 1 below sets `materialize_ttl_after_modify = 0`. Every ALTER is then METADATA ONLY:
--
--   * the rule is recorded and applies to parts written from now on,
--   * existing parts keep their rows until they are next merged, and are expired lazily as normal
--     background merges rewrite them,
--   * nothing is deleted at the moment you run this script.
--
-- That is the whole point. It turns "one statement deletes an unknown number of rows" into "the
-- policy takes effect, and you choose when each partition is enforced".
--
-- HOW TO STAGE IT ON A LIVE INSTALL
--
--   0. Take a backup, or at minimum know your restore path. There is no undo for an expired row.
--   1. Run the COUNT queries in step 0 below. They report, per table, how many rows are already
--      past the proposed horizon, WITHOUT deleting anything. If a number surprises you, stop and
--      change the horizon in tally.storage_tiering rather than running the ALTER.
--   2. Run steps 1 and 2 (the metadata-only ALTERs). Cheap, seconds, no data movement.
--   3. Verify with the step 3 query that every table now carries the expected TTL expression.
--   4. Enforce deliberately, ONE PARTITION AT A TIME, at a quiet hour:
--         ALTER TABLE <table> MATERIALIZE TTL IN PARTITION <id>;
--      Watch system.mutations between partitions. Do NOT run a bare
--      `ALTER TABLE <table> MATERIALIZE TTL`, which does the whole table at once and is the thing
--      step 1 exists to avoid. Partition ids come from
--         SELECT partition, rows FROM system.parts WHERE table = '<t>' AND active ORDER BY partition;
--      last_touch_index and unattributed_events have NO PARTITION BY, so they have a single `tuple()`
--      partition and cannot be staged this way. They are the two to schedule most carefully;
--      last_touch_index is also the largest table here, and expiry on it rewrites whole parts rather
--      than dropping partitions.
--   5. If you skip step 4 entirely, that is a valid choice: the policy still applies to new data and
--      old rows drain away as background merges reach them. It is slower and less predictable, and
--      it is what happens by default if nobody does anything.
--
-- ORDER OF OPERATIONS WITH THE OTHER MIGRATIONS
--
--   * Run this AFTER `make ch-migrate`, so the tables and columns exist.
--   * `make ch-rollup-rebuild` recreates the three rollups with `CREATE TABLE ... AS`, which copies
--     columns, engine and skipping indexes but NOT the TTL (the CTO-245 lesson). That script now
--     re-applies the TTL to its shadow tables explicitly and asserts the engine clauses match, so
--     it is safe to run in either order. Do not remove that re-application.
--   * `make ch-migrate-otel-engine` restores the DEFAULT raw-span TTL at its step 5. If you run a
--     per-tenant override, re-apply it after that migration, and re-apply the matching
--     last_touch_index override with it: that table's horizon is pinned to the raw span horizon.
--
-- PER-TENANT OVERRIDES
--
-- ClickHouse TTL is table-level, so a per-tenant horizon compiles into a multiIf on TenantId. Do not
-- hand-write one. Generate it, exactly as the raw-span override is generated:
--
--   from tally.storage_tiering import render_tenant_delete_expression, retention_for
--   p = retention_for("replay_samples")
--   print(p.render_alter({"<tenant-uuid>": 90}))
--
-- The replay tables are the case that will actually need this, because
-- tenant_replay_config.retention_days is already per tenant in Postgres.

-- ============================================================================================
-- 0. LOOK BEFORE YOU LEAP. Read-only. How many rows are already past each proposed horizon?
--    Run this first, on its own. It deletes nothing.
-- ============================================================================================
-- NOTE: the UNION must be wrapped before ORDER BY. In ClickHouse an ORDER BY written after a
-- UNION ALL binds to the LAST SELECT only, not to the union result, and the alias from the
-- first SELECT is not in scope there, so the bare form fails with UNKNOWN_IDENTIFIER (47).
SELECT * FROM (
SELECT 'daily_feature_rollup'  AS table, count() AS rows_past_horizon FROM daily_feature_rollup  WHERE toDateTime(Day)               < now() - INTERVAL 2555 DAY
UNION ALL SELECT 'daily_account_rollup',  count() FROM daily_account_rollup  WHERE toDateTime(Day)               < now() - INTERVAL 2555 DAY
UNION ALL SELECT 'business_events',       count() FROM business_events       WHERE toDateTime(OccurredAt)        < now() - INTERVAL 2555 DAY
UNION ALL SELECT 'attribution_records',   count() FROM attribution_records   WHERE toDateTime(AttributedTraceTs) < now() - INTERVAL 2555 DAY
UNION ALL SELECT 'hourly_feature_rollup', count() FROM hourly_feature_rollup WHERE toDateTime(Hour)              < now() - INTERVAL 400 DAY
UNION ALL SELECT 'identity_graph',        count() FROM identity_graph        WHERE toDateTime(ObservedAt)        < now() - INTERVAL 400 DAY
UNION ALL SELECT 'unattributed_events',   count() FROM unattributed_events   WHERE toDateTime(OccurredAt)        < now() - INTERVAL 400 DAY
UNION ALL SELECT 'last_touch_index',      count() FROM last_touch_index      WHERE toDateTime(UpdatedAt)         < now() - INTERVAL 90 DAY
UNION ALL SELECT 'business_events.RawPayload (column blanked, row kept)',
                                          count() FROM business_events       WHERE toDateTime(OccurredAt)        < now() - INTERVAL 90 DAY
UNION ALL SELECT 'replay_samples',        count() FROM replay_samples        WHERE toDateTime(CapturedAt)        < now() - INTERVAL 30 DAY
UNION ALL SELECT 'replay_runs',           count() FROM replay_runs           WHERE toDateTime(RanAt)             < now() - INTERVAL 30 DAY
) ORDER BY rows_past_horizon DESC
FORMAT PrettyCompactMonoBlock;

-- ============================================================================================
-- 1. Make the ALTERs metadata-only. This SETTINGS statement is the safety mechanism of this whole
--    file; if you run the ALTERs below without it, ClickHouse deletes on the spot.
--    It is session-scoped, so it must be in the SAME clickhouse-client invocation as the ALTERs.
--    `make ch-apply-retention` runs the file in one --multiquery session, which satisfies that.
-- ============================================================================================
SET materialize_ttl_after_modify = 0;

-- ============================================================================================
-- 2. Record the policy. Each statement is generated by tally.storage_tiering; regenerate rather
--    than editing a number here, or the DDL and the module drift and the SDK test that compares
--    them fails (sdk/python/tests/test_derived_retention_ddl.py).
--
--    Idempotent: MODIFY TTL restates the rule, so replaying this file costs one metadata write.
-- ============================================================================================

-- Class 1: book of record, 7 years.
ALTER TABLE daily_feature_rollup MODIFY TTL toDateTime(Day) + INTERVAL 2555 DAY DELETE;
ALTER TABLE daily_account_rollup MODIFY TTL toDateTime(Day) + INTERVAL 2555 DAY DELETE;
ALTER TABLE business_events MODIFY TTL toDateTime(OccurredAt) + INTERVAL 2555 DAY DELETE;
ALTER TABLE attribution_records MODIFY TTL toDateTime(AttributedTraceTs) + INTERVAL 2555 DAY DELETE;

-- Class 2: operational grain, 13 months.
ALTER TABLE hourly_feature_rollup MODIFY TTL toDateTime(Hour) + INTERVAL 400 DAY DELETE;
ALTER TABLE identity_graph MODIFY TTL toDateTime(ObservedAt) + INTERVAL 400 DAY DELETE;
ALTER TABLE unattributed_events MODIFY TTL toDateTime(OccurredAt) + INTERVAL 400 DAY DELETE;

-- Class 3: follows raw spans, 90 days.
-- If this deployment runs a per-tenant raw-retention override, last_touch_index needs the SAME
-- multiIf, not this default. Generate both from tally.storage_tiering and apply them together.
ALTER TABLE last_touch_index MODIFY TTL toDateTime(UpdatedAt) + INTERVAL 90 DAY DELETE;

-- The column TTL. Blanks RawPayload at 90 days and keeps the row, and every monetary column on it,
-- for the full 7 years of the table TTL above. MODIFY COLUMN restates the full type because
-- ClickHouse requires it on this form; the type and codec are unchanged from attribution.sql.
ALTER TABLE business_events MODIFY COLUMN RawPayload String CODEC(ZSTD(3)) TTL toDateTime(OccurredAt) + INTERVAL 90 DAY;

-- Class 4: opt-in replay corpus, 30 days, the horizon tenant_replay_config.retention_days already
-- promises. Replace both with a generated multiIf if any tenant has a non-default retention_days.
ALTER TABLE replay_samples MODIFY TTL toDateTime(CapturedAt) + INTERVAL 30 DAY DELETE;
ALTER TABLE replay_runs MODIFY TTL toDateTime(RanAt) + INTERVAL 30 DAY DELETE;

-- eval_runs is LAST on purpose, and it is the one statement here that may legitimately fail.
-- db/clickhouse/eval_runs.sql is neither mounted into docker-entrypoint-initdb.d nor listed in the
-- Makefile's CH_DDL, so a stack that has not applied it by hand simply has no eval_runs table, and
-- ClickHouse has no ALTER TABLE IF EXISTS to express that. Ordering it after everything else means
-- an UNKNOWN_TABLE here aborts nothing that matters: every other TTL above is already recorded.
-- If it fails and you do run the eval harness, apply db/clickhouse/eval_runs.sql first (its CREATE
-- TABLE now carries the same TTL inline) and re-run this file.
ALTER TABLE eval_runs MODIFY TTL toDateTime(JudgedAt) + INTERVAL 400 DAY DELETE;

-- ============================================================================================
-- 3. Verify. Every managed table should now show its TTL inside engine_full, and NOTHING should
--    have been deleted yet: re-run the step 0 counts and they should be unchanged.
-- ============================================================================================
SELECT
    name,
    extract(engine_full, 'TTL .*$') AS ttl_clause
FROM system.tables
WHERE database = currentDatabase()
  AND name IN ('daily_feature_rollup', 'hourly_feature_rollup', 'daily_account_rollup',
               'business_events', 'attribution_records', 'identity_graph', 'unattributed_events',
               'last_touch_index', 'replay_samples', 'replay_runs', 'eval_runs')
ORDER BY name
FORMAT PrettyCompactMonoBlock;

-- Any table in that list with an empty ttl_clause did not take the ALTER. The usual cause is that
-- the table does not exist on this install: eval_runs in particular is NOT mounted into
-- docker-entrypoint-initdb.d and is NOT in the Makefile's CH_DDL list, so a stack that never ran
-- the eval harness has no such table and its ALTER above will have failed. That gap predates this
-- change and is not fixed here.
