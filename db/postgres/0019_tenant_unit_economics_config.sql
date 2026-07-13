-- Per-tenant LTV/CAC band thresholds for the unit-economics view (CTO-126).
--
-- web/lib/unitEconomics.ts classifies the LTV:CAC ratio and payback months into green/yellow/red
-- bands. Those cutoffs were hardcoded B2B-SaaS defaults ("tenant-configurable in v2"). This table
-- IS v2: one row per tenant overrides some or all of the four cutoffs; a tenant with no row keeps
-- the hardcoded defaults. The defaults remain the fallback everywhere — the row only shifts the
-- boundaries a given tenant wants.
--
-- Semantics (mirrors the lib's classify helpers):
--   * ltv_cac_green_threshold  — ratio strictly ABOVE this is green (default 3.0)
--   * ltv_cac_yellow_threshold — ratio at/above this (but not green) is yellow (default 1.0); below is red
--   * payback_months_green     — payback at/below this many months is green (default 12)
--   * payback_months_yellow    — payback at/below this (but not green) is yellow (default 18); above is red
--
-- Higher LTV:CAC is better, so green_threshold >= yellow_threshold. Lower payback is better, so
-- payback_green <= payback_yellow. The CHECK constraints encode both so a fat-fingered inversion is
-- rejected at the DB rather than silently mis-coloring the dashboard.
--
-- Reads/writes go through GET/POST /v1/tenant/unit-economics/config — the web app never touches
-- Postgres directly (same rule as tenant_guardrails / cac_periods). Companion table
-- tenant_unit_economics_config_changes is the audit trail: every upsert appends one row keyed by a
-- client-supplied change_id UUID, so a retried request is idempotent (both the config write and the
-- audit row are no-ops on replay).
--
-- Migration is additive: existing tenants have zero rows, which the reader treats as "use defaults".

CREATE TABLE IF NOT EXISTS tenant_unit_economics_config (
    tenant_id                 UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    ltv_cac_green_threshold   NUMERIC(12, 4) NOT NULL,
    ltv_cac_yellow_threshold  NUMERIC(12, 4) NOT NULL,
    payback_months_green      NUMERIC(12, 4) NOT NULL,
    payback_months_yellow     NUMERIC(12, 4) NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by                TEXT,
    -- Higher LTV:CAC is healthier; lower payback is healthier. Reject inverted bands.
    CHECK (ltv_cac_green_threshold >= ltv_cac_yellow_threshold),
    CHECK (payback_months_green <= payback_months_yellow),
    CHECK (ltv_cac_yellow_threshold >= 0 AND payback_months_green >= 0)
);

CREATE TABLE IF NOT EXISTS tenant_unit_economics_config_changes (
    change_id   UUID NOT NULL,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor       TEXT,
    before      JSONB,
    after       JSONB,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, change_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_unit_economics_config_changes_tenant
    ON tenant_unit_economics_config_changes(tenant_id, changed_at DESC);
