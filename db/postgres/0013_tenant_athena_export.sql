-- Per-tenant opt-in for the S3 / Athena export sink (CTO-160).
--
-- The AWS analog of the BigQuery export (CTO-154, migration 0009). The export worker mirrors a
-- tenant's telemetry (spans / business_events / attribution / daily rollups) into the tenant's OWN
-- S3 bucket as partitioned Parquet, queryable via an Athena external table (or Glue catalog) and
-- loadable into Redshift with a COPY recipe. ClickHouse stays the primary store and the dashboard
-- keeps reading it — this is an additive mirror, never a replacement.
--
-- Opt-in and off by default: `enabled=false`, and a tenant with no row exports nothing. Existing
-- tenants don't suddenly start mirroring on upgrade.
--
-- `bucket` / `prefix` / `database` name the destination (S3 prefix + Athena/Glue database); `region`
-- is the AWS region. `credential_ref` is a *reference*, not a secret: an IAM role ARN the worker
-- assumes, or a Secrets Manager / SSM parameter resource name. The gateway rejects inline AWS access
-- keys / secrets here (`tenant_athena_export._reject_raw_key`) — raw keys never live in this table.
--
-- `tenant_id` is plain TEXT (not a UUID FK) because the export worker keys off the same TenantId
-- string the telemetry path uses end-to-end, exactly like `tenant_bq_export_config`.

CREATE TABLE IF NOT EXISTS tenant_athena_export_config (
    tenant_id      TEXT PRIMARY KEY,
    enabled        BOOLEAN NOT NULL DEFAULT false,
    bucket         TEXT NOT NULL DEFAULT '',
    prefix         TEXT NOT NULL DEFAULT 'tally/',
    database       TEXT NOT NULL DEFAULT 'tally_export',
    region         TEXT NOT NULL DEFAULT '',
    credential_ref TEXT NOT NULL DEFAULT '',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When enabled, a destination is required.
    CONSTRAINT athena_export_enabled_needs_destination
        CHECK (enabled = false OR (bucket <> '' AND database <> ''))
);
