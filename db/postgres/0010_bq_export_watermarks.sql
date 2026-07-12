-- Incremental export cursor for the BigQuery export sink (CTO-154).
--
-- One row per (tenant, source table). `watermark` is the high-water value of that table's
-- incremental column (spans → Timestamp, business_events → OccurredAt, attribution_records →
-- AttributedTraceTs, daily_feature_rollup → Day) that has been exported so far. Each export pass
-- reads rows strictly greater than the stored watermark, upserts them into BigQuery on the same
-- natural key ClickHouse dedupes on, then advances the watermark to the max value it saw.
--
-- A missing row means "never exported" and reads back as NULL — the first pass is a full backfill.
-- Because the sink upserts on the natural key, a re-run over the same window is idempotent and
-- never duplicates rows, so it is safe to replay a pass after a crash.

CREATE TABLE IF NOT EXISTS bq_export_watermarks (
    tenant_id     TEXT NOT NULL,
    source_table  TEXT NOT NULL,
    watermark     TIMESTAMPTZ NOT NULL,
    rows_exported BIGINT NOT NULL DEFAULT 0
                      CHECK (rows_exported >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source_table)
);
