-- Control-plane schema (Postgres). Implements CTO-27. Spec §5.2.
--
-- Transactional config/state that does NOT belong in ClickHouse. Secrets are stored as KMS
-- references only (never raw key material). Everything is tenant-scoped.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- Tenants ----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    region              TEXT NOT NULL,                 -- data residency (us-east, eu-west, ...)
    plan                TEXT NOT NULL DEFAULT 'free',
    hash_salt_kek_ref   TEXT NOT NULL,                 -- KMS reference for the per-tenant HMAC key set
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT no_raw_secret CHECK (hash_salt_kek_ref NOT LIKE 'sk-%' AND length(hash_salt_kek_ref) < 512)
);

-- API keys (scoped, tenant-bound). We store only a hash of the key, never the key itself. --------
CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash      TEXT NOT NULL UNIQUE,                -- SHA-256 of the token
    scope         TEXT NOT NULL DEFAULT 'write' CHECK (scope IN ('read','write','admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);

-- Feature tags (declared by the customer) --------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_tags (
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tag           TEXT NOT NULL,
    description   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, tag)
);

-- Value events (customer-defined → attribution config) -------------------------------------------
CREATE TABLE IF NOT EXISTS value_events (
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_name       TEXT NOT NULL,
    feature_tag      TEXT NOT NULL,
    lookback_days    INT NOT NULL DEFAULT 30 CHECK (lookback_days > 0),
    attribution_model TEXT NOT NULL DEFAULT 'last_touch_v1',
    PRIMARY KEY (tenant_id, event_name, feature_tag)
);

-- Guardrails -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guardrails (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    scope                 TEXT NOT NULL,               -- feature tag or agent name
    mode                  TEXT NOT NULL DEFAULT 'observe'
                              CHECK (mode IN ('observe','warn','graceful','hard_stop')),
    max_cost_micro_usd    BIGINT,
    max_steps             INT,
    max_tool_calls        INT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_guardrails_tenant ON guardrails(tenant_id);

-- Connector configs (CDP webhooks, cloud billing, vector DB) -------------------------------------
CREATE TABLE IF NOT EXISTS connector_configs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,                      -- 'segment','stripe','aws_cost','pinecone',...
    config         JSONB NOT NULL DEFAULT '{}'::jsonb, -- non-secret config; secrets are KMS refs
    secret_kek_ref TEXT,                               -- KMS reference, never raw
    enabled        BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_connectors_tenant ON connector_configs(tenant_id);

-- Plan tiers + usage limits (self-serve, CTO-89) -------------------------------------------------
CREATE TABLE IF NOT EXISTS plan_tiers (
    name                TEXT PRIMARY KEY,              -- 'free','pro','scale'
    max_traces_per_month BIGINT,
    max_features         INT,
    price_micro_usd      BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_limits (
    tenant_id   UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    plan        TEXT NOT NULL REFERENCES plan_tiers(name),
    overrides   JSONB NOT NULL DEFAULT '{}'::jsonb     -- per-tenant limit overrides
);

-- Per-tenant price overrides (enterprise contracts; mirrors PriceCatalog overrides, CTO-54) ------
CREATE TABLE IF NOT EXISTS price_catalog_overrides (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    price_type    TEXT NOT NULL,
    unit          TEXT NOT NULL,
    price_per_unit NUMERIC(20,8) NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    valid_from    DATE NOT NULL,
    valid_to      DATE
);
CREATE INDEX IF NOT EXISTS idx_price_overrides_tenant ON price_catalog_overrides(tenant_id);
