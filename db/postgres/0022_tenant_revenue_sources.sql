-- Per-tenant revenue source configuration for the attribution view (CTO-194).
--
-- queryAttribution used to sum revenue with a hardcoded `business_events.Source = 'stripe'` filter.
-- `Source` is an unconstrained LowCardinality(String) — whatever string the ingesting connector
-- happened to stamp on the row — so any tenant billing through something other than Stripe (or
-- ingesting through a CDP, a backfill script, or a self-hosted biller) had every revenue event
-- silently discarded and the VALUE/USER and MARGIN/USER columns rendered blank.
--
-- The correct discriminator is `business_events.ValueType`, which is a real enum:
-- ('monetary'=1,'count'=2,'mrr'=3,'refund'=4). Engagement signals are 'count' and carry no amount;
-- money is 'monetary' / 'mrr'; 'refund' must NET OFF rather than be ignored. This table narrows the
-- ValueType-based default by naming which SOURCES a tenant considers revenue-bearing, for tenants
-- who ingest monetary events from more than one system and only want some of them counted.
--
-- Defaults, applied when a tenant has NO row (so no existing tenant is broken by this migration):
--   * every source counts (revenue_sources = NULL means "do not filter by Source")
--   * ValueType IN ('monetary','mrr') counts as revenue, 'refund' subtracts, 'count' is ignored
-- A row with a non-empty revenue_sources array restricts the sum to those sources; the ValueType
-- rules are unchanged, because ValueType is the discriminator and Source is only ever a narrowing.
--
-- include_mrr exists because summing recurring 'mrr' amounts alongside one-off 'monetary' charges
-- double counts for tenants whose biller emits both for the same subscription. Default TRUE keeps
-- today's behaviour of counting everything monetary the tenant actually receives.
--
-- Reads/writes go through GET/POST /v1/tenant/revenue-sources/config — the web app never touches
-- Postgres directly (same rule as tenant_unit_economics_config / tenant_guardrails). Companion
-- table tenant_revenue_source_config_changes is the audit trail: every upsert appends one row keyed
-- by a client-supplied change_id UUID, so a retried request is idempotent (both the config write and
-- the audit row are no-ops on replay).

CREATE TABLE IF NOT EXISTS tenant_revenue_source_config (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    -- NULL = every business_events.Source counts (the default). A non-empty array restricts the
    -- revenue sum to those Source values. An empty array is rejected: "no source counts as revenue"
    -- is indistinguishable from a misconfiguration and would silently blank the dashboard, which is
    -- exactly the bug this table exists to fix.
    revenue_sources  TEXT[],
    include_mrr      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT,
    CHECK (revenue_sources IS NULL OR array_length(revenue_sources, 1) >= 1)
);

CREATE TABLE IF NOT EXISTS tenant_revenue_source_config_changes (
    change_id   UUID NOT NULL,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor       TEXT,
    before      JSONB,
    after       JSONB,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, change_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_revenue_source_config_changes_tenant
    ON tenant_revenue_source_config_changes(tenant_id, changed_at DESC);
