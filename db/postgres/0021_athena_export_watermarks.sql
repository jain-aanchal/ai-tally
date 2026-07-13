-- Incremental export cursor for the S3 / Athena export sink (CTO-160).
--
-- The AWS analog of `bq_export_watermarks` (CTO-154, migration 0010). One row per (tenant, source
-- table). `watermark` is the high-water value of that table's incremental column (spans → Timestamp,
-- business_events → OccurredAt, attribution_records → AttributedTraceTs, daily_feature_rollup → Day)
-- exported so far. Each pass reads rows strictly greater than the stored watermark, writes them to
-- the matching S3 day partition(s) as Parquet, then advances the watermark to the max value it saw.
--
-- A missing row means "never exported" and reads back as NULL — the first pass is a full backfill.
-- Idempotency is per day partition: a partition write upserts the partition's rows on the same
-- natural key ClickHouse dedupes on, so replaying a pass after a crash never duplicates rows.

CREATE TABLE IF NOT EXISTS athena_export_watermarks (
    tenant_id     TEXT NOT NULL,
    source_table  TEXT NOT NULL,
    watermark     TIMESTAMPTZ NOT NULL,
    rows_exported BIGINT NOT NULL DEFAULT 0
                      CHECK (rows_exported >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source_table)
);
