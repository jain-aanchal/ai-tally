-- CTO-245: move an EXISTING otel_spans from MergeTree to ReplacingMergeTree with span identity in
-- the sorting key. EXPLICIT, one-shot, operator-run. Not part of `make ch-migrate`.
--
-- WHY THIS IS A SEPARATE FILE AND NOT PART OF THE CANONICAL DDL.
--
-- A table's ENGINE and ORDER BY cannot be changed with ALTER. ClickHouse has no statement for it,
-- so the only real migration is: build the new table, move the data, swap the names. That is a full
-- copy of the raw span table and it is the one operation in this repo that is neither cheap nor
-- idempotent-by-restatement, so it must not run implicitly. `db/clickhouse/otel_spans.sql` uses
-- CREATE TABLE IF NOT EXISTS, which means a FRESH database gets the correct engine straight away
-- and an EXISTING one is left untouched by a replay of that file. This script is how an existing
-- install catches up, and running it is a deliberate act.
--
-- CHECK WHICH ENGINE YOU ARE ACTUALLY ON before and after. Do not assume from the DDL file:
--
--   SELECT engine, sorting_key FROM system.tables
--    WHERE database = currentDatabase() AND name = 'otel_spans';
--
-- A fixed install reads `ReplacingMergeTree` with `... Timestamp, TraceId, SpanId`. Anything else,
-- including a ReplacingMergeTree whose sorting key does NOT end in TraceId, SpanId, is not fixed:
-- see otel_spans.sql for why deduping on the old key would delete distinct spans.
--
-- BEFORE YOU RUN IT.
--
--   * Take a backup or a snapshot. This swaps your raw span table.
--   * Have enough free disk for a SECOND full copy of otel_spans while step 2 runs. Check with
--     `SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts
--       WHERE table = 'otel_spans' AND active`.
--   * Step 2 is a bulk copy and takes as long as the table is large. Ingest continues throughout,
--     but rows written into the OLD table after step 2 starts are not carried over. Quiesce ingest
--     for the duration, or accept losing that window and re-post it.
--   * If you have applied a per-tenant retention override (see storage_tiering.sql, the
--     `ALTER TABLE otel_spans MODIFY TTL multiIf(...)` form), step 5 below restores the DEFAULT
--     policy and your override is gone. Re-apply it afterwards. CREATE TABLE ... AS copies columns
--     and skipping indexes but NOT the TTL, which is why step 5 exists at all.
--
-- WHAT IT DOES NOT DO. It does not repair the rollup materialized views. Duplicates that reached
-- ClickHouse were summed into daily_feature_rollup / hourly_feature_rollup / daily_account_rollup at
-- INSERT time, and collapsing the raw table does not subtract them. Rebuilding those targets from
-- the deduplicated raw table is a separate operation, only possible while the raw rows are still
-- inside the 90-day retention, and it is deliberately not attempted here. It has its own scripts,
-- and this one is their prerequisite because their notion of truth is a FINAL read of the table
-- this script gives you: db/clickhouse/checks/rollup_drift.sql (`make ch-rollup-check`, read-only)
-- to measure the damage, and db/clickhouse/migrations/rollup_rebuild_from_spans.sql
-- (`make ch-rollup-rebuild`) to repair the grains raw can still account for. See CTO-311.

-- 1. The new table. Same columns and skipping indexes as the live one, correct engine, and the
--    sorting key extended with TraceId, SpanId so the collapsing identity is one span.
CREATE TABLE IF NOT EXISTS otel_spans_cto245 AS otel_spans
ENGINE = ReplacingMergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (TenantId, FeatureTag, ServiceName, SpanName, Timestamp, TraceId, SpanId);

-- 2. Move the data. The duplicates come across as-is and are collapsed by step 4, not here: an
--    INSERT ... SELECT DISTINCT would need a full sort of the table in memory and would also make
--    the copy silently lossy if any assumption about identity were wrong.
INSERT INTO otel_spans_cto245 SELECT * FROM otel_spans;

-- 3. Swap. EXCHANGE TABLES is atomic, so there is no window where `otel_spans` does not exist.
--    Verified against ClickHouse 24.8: the materialized views reading FROM otel_spans keep firing
--    after the exchange, because they resolve their source by name.
EXCHANGE TABLES otel_spans AND otel_spans_cto245;

-- 4. Collapse what was already duplicated. ReplacingMergeTree only deduplicates when parts merge,
--    so without this the historical duplicates sit there until a background merge happens to reach
--    them. FINAL forces it now, across every partition. On a large table this is the expensive step.
OPTIMIZE TABLE otel_spans FINAL;

-- 5. Restore the tiering TTL, which CREATE TABLE ... AS did not copy. This is the DEFAULT policy
--    from tally.storage_tiering.DEFAULT_POLICY, identical to the clause in otel_spans.sql. If you
--    had a per-tenant override, re-apply it after this.
ALTER TABLE otel_spans MODIFY TTL
    toDateTime(Timestamp) + INTERVAL 7 DAY  TO VOLUME 'warm',
    toDateTime(Timestamp) + INTERVAL 30 DAY TO VOLUME 'cold',
    toDateTime(Timestamp) + INTERVAL 90 DAY DELETE;

-- 6. Verify, THEN drop the old table. `otel_spans_cto245` now holds the pre-migration data (the
--    exchange swapped the names), so it is your rollback until you drop it. Confirm the counts
--    before you do:
--
--   SELECT count() AS rows, uniqExact(TenantId, TraceId, SpanId) AS spans FROM otel_spans;
--
--   rows should now equal spans. If it does not, a merge is still running (re-run step 4) or some
--   duplicate pair is not byte-identical (see the third caveat in otel_spans.sql).
--
--   DROP TABLE otel_spans_cto245;
