-- Per-tenant cloud compute-billing connector config (CTO-143).
--
-- The Compute column on /cost is $0 on every live deployment because no cloud-billing data ever
-- reaches otel_spans. The daily compute connector (gateway.connectors.compute) fixes that: it pulls
-- a tenant's cloud compute spend (AWS Cost Explorer or GCP Cloud Billing) and lands it as synthetic
-- `compute`-layer spans. This table is the per-tenant control-plane row that connector reads.
--
-- One provider per tenant for v1 (aws | gcp — Azure is out of scope). ``tag_filter`` narrows the
-- billing query to the tenant's AI workload (default {"tally:workload":"ai"}) so we don't sweep in
-- unrelated infra spend.
--
-- Secrets by REFERENCE only (CTO-143 hard rule): ``credentials_ref`` is a Secret Manager / KMS /
-- ARN pointer (or the literal 'aws-default-chain' to use the ambient AWS credential chain) — never
-- raw keys. Mirrors how tenants.hash_salt_kek_ref (0001) and the Stripe secret note (0003) work.
--
-- Run status lives on this same row (last_run_at / last_status), mirroring the reconciliation_runs
-- (0008) + tenant_integration_runs (0007) pattern: the connector calls record_run after each cycle.
-- A failed fetch stamps 'failed' and emits NO span (honest-under-uncertainty — never a guessed number).
--
-- Migration is additive: existing tenants have zero rows, which the connector treats as
-- "compute connector not configured" and skips.

CREATE TABLE IF NOT EXISTS tenant_compute_config (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    cloud_provider   TEXT NOT NULL CHECK (cloud_provider IN ('aws', 'gcp')),
    -- Secret Manager / KMS / ARN reference — NEVER raw credentials. Bounded length so a fat-fingered
    -- raw key (which is long) is more likely to trip the check than land silently.
    credentials_ref  TEXT NOT NULL CHECK (length(credentials_ref) > 0 AND length(credentials_ref) < 512),
    -- Cost-allocation tag set the billing query is filtered to. Default targets the AI workload.
    tag_filter       JSONB NOT NULL DEFAULT '{"tally:workload": "ai"}'::jsonb,
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT CHECK (last_status IN ('success', 'partial', 'failed')),
    connected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenant_compute_config_tenant
    ON tenant_compute_config(tenant_id);
