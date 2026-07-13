-- Per-tenant feature value-event config (CTO-140).
--
-- Onboarding wires each feature's ROI to a business "value event" (e.g. subscription_created). Each
-- row pins one value event to one (tenant, feature) — the config the /features attribution reads
-- against. Companion table `tenant_feature_value_event_changes` is an audit trail: every upsert or
-- delete appends one row keyed by a client-supplied `change_id` for idempotent replay.
--
-- Reads and writes both go through GET/POST/DELETE /v1/tenant/feature-value-events — the web app
-- never touches Postgres directly. Same shape as the guardrail control plane (CTO-116).

CREATE TABLE IF NOT EXISTS tenant_feature_value_events (
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    feature_tag  TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   TEXT,
    notes        TEXT,
    PRIMARY KEY (tenant_id, feature_tag)
);

CREATE INDEX IF NOT EXISTS idx_tenant_feature_value_events_tenant
    ON tenant_feature_value_events(tenant_id);

CREATE TABLE IF NOT EXISTS tenant_feature_value_event_changes (
    change_id    UUID NOT NULL,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    feature_tag  TEXT NOT NULL,
    actor        TEXT,
    before       JSONB,
    after        JSONB,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, change_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_feature_value_event_changes_tenant_feature
    ON tenant_feature_value_event_changes(tenant_id, feature_tag, changed_at DESC);
