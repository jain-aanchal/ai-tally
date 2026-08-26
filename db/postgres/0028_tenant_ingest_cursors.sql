-- Incremental ingest cursors for the Segment / HubSpot / Pendo workers (CTO-219).
--
-- WHY this table exists. The scheduled ingest workers (CTO-216) had no cursor, so every cycle asked
-- the provider for its whole default payload: 96 identical pulls a day for Segment and HubSpot, 48
-- for Pendo. This table is the per-tenant per-integration watermark that turns each cycle into
-- "give me what is new since X".
--
-- WHAT THIS IS NOT FIXING, precisely, because the cost was overstated when it was first written up.
-- Re-sending the same payload did NOT corrupt data. `BusinessEventId` is the PROVIDER's stable id
-- (Segment `messageId`, Stripe event `id`, HubSpot `eventId`; Pendo has none, so the mapper builds a
-- deterministic (feature, hashed visitor) id), and `business_events` is
-- `ReplacingMergeTree(IngestedAt) ORDER BY (TenantId, BusinessEventId)`, so re-insertion of the same
-- event collapses to one row by design. The real costs are narrower and both real:
--
--   * Write amplification. Every cycle re-inserts every event in the provider's window, and every
--     one of those rows is stored, merged and then discarded by the background merge.
--   * A transient pre-merge double count. Between the insert and the merge that collapses it, a
--     read WITHOUT `FINAL` sees the same event more than once. Only 2 of the 12 `business_events`
--     reads in `web/lib/clickhouse.ts` use `FINAL`, so most surfaces are exposed to that window.
--
-- SHAPE. Follows `bq_export_watermarks` (0010), which is the existing precedent for a per-tenant
-- incremental cursor in this control plane: one row per (tenant, source), a timestamp high-water
-- mark, and a missing row meaning "never run". A missing row is NOT "fetch all history": the worker
-- floors a first run at a bounded initial window (see gateway.ingest_cursors), because a tenant
-- connecting Segment for the first time should not trigger an unbounded backfill on a 15-minute
-- cadence.
--
-- The cursor is deliberately BEHIND the last event seen by a per-connector overlap (Pendo's is the
-- largest, because its aggregation API is documented at ~5 minutes of latency). Re-fetching that
-- overlap every cycle is safe precisely because of the ReplacingMergeTree dedup above, and losing an
-- event that the provider had not aggregated yet would not be.
--
-- Migration is additive and every existing deployment has zero rows, which reads as "no tenant has
-- ever run an incremental cycle" and is true: nothing scheduled these workers before CTO-216, and
-- the scheduler itself is still opt-in behind TALLY_SCHEDULER_ENABLED.

CREATE TABLE IF NOT EXISTS tenant_ingest_cursors (
    tenant_id    TEXT NOT NULL CHECK (length(tenant_id) > 0),
    connector_id TEXT NOT NULL
                     CHECK (connector_id IN ('segment', 'hubspot', 'pendo')),
    -- High-water mark: the next cycle asks the provider for events at or after this instant.
    cursor_at    TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connector_id)
);

-- tenant_id is TEXT with no FK to tenants, matching scheduler_runs (0027) and reconciliation_runs
-- (0008). A cursor that outlives its tenant row is harmless (nothing reads it), whereas a cascade
-- during a cycle would delete the cursor out from under a run in flight and silently turn the next
-- cycle into a full initial-window pull.
