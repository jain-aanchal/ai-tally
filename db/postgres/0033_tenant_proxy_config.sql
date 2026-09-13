-- 0033_tenant_proxy_config.sql
-- Per-organization on/off switch for the hosted edge proxy (zero-code connect).
--
-- WHY A SETTING AND NOT JUST A DEPLOY FLAG. The operator decides whether a proxy runs at all
-- (deploy/demo: INGEST_DOMAIN). Each organization then decides whether its keys may be used through
-- it. Routing production LLM traffic through a third party's server is a trust decision an org admin
-- should make explicitly, so the default is OFF, and a key that is valid for the SDK is refused by the
-- proxy until an admin turns the proxy on under Settings > API keys.
--
-- HOW THE PROXY LEARNS ABOUT A CHANGE. The proxy never queries this table. It caches key metadata
-- from the gateway's /v1/edge/keys delta feed, which pages api_keys by a watermark. A toggle changes
-- no api_keys row, so on its own it would never reach the feed. edge_updated_at fixes that: the
-- toggle stamps it on the org's live keys in the same transaction, which raises their watermark, so
-- they re-enter the feed carrying the new proxy_enabled value within one proxy refresh (45s).
--
-- IF NOT EXISTS throughout so replaying the migration set stays idempotent.

CREATE TABLE IF NOT EXISTS tenant_proxy_config (
    tenant_id  UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    enabled    BOOLEAN NOT NULL DEFAULT false,
    -- The Clerk user id that last changed it, for audit. Bounded like every other free-text column.
    updated_by TEXT CHECK (updated_by IS NULL OR length(updated_by) <= 128),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS edge_updated_at TIMESTAMPTZ;

-- The feed's watermark now includes edge_updated_at, and 0030's index is over the old expression, so
-- the planner would stop using it and every poll would be a sequential scan again. Replace it with an
-- index over the new expression. It MUST match gateway/edge_keys.py _WATERMARK_SQL exactly.
DROP INDEX IF EXISTS idx_api_keys_edge_watermark;
CREATE INDEX IF NOT EXISTS idx_api_keys_edge_watermark_v2
    ON api_keys (
        GREATEST(created_at, COALESCE(revoked_at, created_at), COALESCE(edge_updated_at, created_at)),
        id
    );
