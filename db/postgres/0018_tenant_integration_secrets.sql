-- Per-tenant credentials-by-reference for the CDP / CRM / product-analytics ingest workers
-- (CTO-127).
--
-- Segment, HubSpot and Pendo each need a per-tenant credential to pull events: Segment a source
-- write-key, HubSpot an OAuth / app token, Pendo an integration key. Following the same tradeoff
-- documented in 0003_tenant_stripe_config.sql, this table stores a *reference* to the secret
-- (a Secret Manager / Vault / KMS handle) — never the raw key. The worker dereferences the handle
-- at run time through an injected ``SecretResolver`` (see gateway.tenant_integration_secrets), so
-- the raw credential never lands in the control plane and never appears in a log line.
--
-- ``config`` carries only the NON-secret knobs a connector needs (Segment source base-url, HubSpot
-- portal id, Pendo region base-url) as JSONB. No credentials go in here.
--
-- One row per (tenant, connector). ``connector_id`` is constrained to the three workers this
-- migration introduces; the run-status CHECK in 0007_tenant_integration_runs.sql already allows
-- these three ids, so no change is needed there.

CREATE TABLE IF NOT EXISTS tenant_integration_secrets (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    connector_id    TEXT NOT NULL
                        CHECK (connector_id IN ('segment', 'hubspot', 'pendo')),
    secret_ref      TEXT NOT NULL
                        CHECK (length(secret_ref) > 0 AND length(secret_ref) < 512),
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    connected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    disconnected_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, connector_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_integration_secrets_tenant
    ON tenant_integration_secrets(tenant_id);
