-- Per-tenant opt-in for the BigQuery export sink (CTO-154).
--
-- The export worker mirrors a tenant's telemetry (spans / business_events / attribution / daily
-- rollups) into the tenant's OWN BigQuery dataset for enterprise analytics. ClickHouse stays the
-- primary store and the dashboard keeps reading it — this is an additive mirror, never a
-- replacement.
--
-- Opt-in and off by default: `enabled=false`, and a tenant with no row exports nothing. Existing
-- tenants don't suddenly start mirroring on upgrade.
--
-- `project_id` / `dataset` / `table_prefix` name the destination. `credential_ref` is a
-- *reference*, not a secret: an ADC hint, a Workload-Identity service-account email, or a Secret
-- Manager resource name. The gateway rejects inline service-account keys here
-- (`tenant_bq_export._reject_raw_key`) — raw keys never live in this table.
--
-- `tenant_id` is plain TEXT (not a UUID FK) because the export worker keys off the same TenantId
-- string the telemetry path uses end-to-end, exactly like `reconciliation_runs`.

CREATE TABLE IF NOT EXISTS tenant_bq_export_config (
    tenant_id      TEXT PRIMARY KEY,
    enabled        BOOLEAN NOT NULL DEFAULT false,
    project_id     TEXT NOT NULL DEFAULT '',
    dataset        TEXT NOT NULL DEFAULT 'tally_export',
    table_prefix   TEXT NOT NULL DEFAULT 'tally_',
    credential_ref TEXT NOT NULL DEFAULT '',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When enabled, a destination is required.
    CONSTRAINT bq_export_enabled_needs_destination
        CHECK (enabled = false OR (project_id <> '' AND dataset <> ''))
);
