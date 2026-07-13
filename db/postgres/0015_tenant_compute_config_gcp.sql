-- GCP Cloud Billing fields for the compute connector (CTO-150).
--
-- CTO-143 shipped tenant_compute_config with the AWS path fully wired (cloud_provider already
-- accepts 'gcp' via its CHECK). GCP is different in one way: it has no fine-grained REST cost API,
-- so the source of truth is the Cloud Billing *BigQuery export* table. Two additive columns carry
-- that GCP-specific config; the AWS path leaves both NULL/default and is unaffected.
--
--   * bq_billing_export_table — the fully-qualified export table id (project.dataset.table) the GCP
--     source queries. NULL for AWS tenants; required (non-empty) for a GCP tenant to fetch anything
--     (the connector raises on a blank table, recording a 'failed' run and emitting no span).
--   * label_filter — the GCP label set the export query is scoped to. GCP label keys can't contain
--     ':' , so the AI workload uses a '-' ({"tally-workload":"ai"}), unlike the AWS tag_filter's
--     {"tally:workload":"ai"}. Default matches the connector's DEFAULT_GCP_LABEL_FILTER.
--
-- Auth stays by REFERENCE only (CTO-143 hard rule): credentials_ref is a Secret Manager pointer, or
-- the deployment authenticates the BigQuery client via ADC / Workload Identity — never raw keys.
--
-- Additive & idempotent: ADD COLUMN IF NOT EXISTS, existing rows keep working (AWS unchanged).

ALTER TABLE tenant_compute_config
    ADD COLUMN IF NOT EXISTS bq_billing_export_table TEXT
        CHECK (bq_billing_export_table IS NULL OR length(bq_billing_export_table) < 1024),
    ADD COLUMN IF NOT EXISTS label_filter JSONB NOT NULL DEFAULT '{"tally-workload": "ai"}'::jsonb;
