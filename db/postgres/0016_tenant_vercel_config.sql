-- Per-tenant Vercel compute + egress connector config (CTO-163).
--
-- Vercel-hosted AI apps pay Vercel for BOTH Functions compute (invocations + GB-hours) and bandwidth
-- (egress). CTO-143 built the compute layer (AWS/GCP) and CTO-144 the egress layer (Vercel /
-- Cloudflare / AWS). This ticket adds Vercel to the COMPUTE layer: the daily connector
-- (gateway.connectors.vercel) pulls a tenant's Vercel usage/billing and lands the Functions compute
-- spend as synthetic `compute`-layer spans, so a Vercel app's full infra cost sits next to its LLM
-- spend on /cost. This table is the per-tenant control-plane row that connector reads.
--
-- One row per tenant (a tenant has one Vercel account/team for this integration) -> tenant_id PK,
-- sibling of 0011_tenant_compute_config.
--
-- Secrets by REFERENCE only (same hard rule as 0011/0012): ``access_token_ref`` is a Secret Manager /
-- KMS reference to the Vercel access token — NEVER the raw token. ``team_id`` / ``project_id`` are the
-- PUBLIC Vercel identifiers the usage query is scoped to, not secrets.
--
-- EGRESS DOUBLE-COUNT RECONCILIATION with CTO-144: CTO-144's egress connector already names Vercel
-- bandwidth as an egress source. To avoid double-counting the /cost Egress column, this connector
-- emits Vercel egress ONLY when ``emit_egress = true`` (default FALSE). Default: this connector owns
-- the compute half and Vercel egress flows solely through CTO-144's tenant_egress_config path. Set
-- ``emit_egress = true`` ONLY for a tenant that has NO tenant_egress_config row for
-- egress_provider='vercel', so exactly one path emits. (Defence in depth: when it does emit egress it
-- reuses CTO-144's connector, producing the IDENTICAL synthetic span id, so the base span_exists
-- guard collapses any overlap to one row regardless.)
--
-- Run status lives on this same row (last_run_at / last_status), mirroring 0011/0012. A failed fetch
-- stamps 'failed' and emits NO span (honest-under-uncertainty — never a guessed number).
--
-- Migration is additive: existing tenants have zero rows, which the connector treats as
-- "Vercel connector not configured" and skips.

CREATE TABLE IF NOT EXISTS tenant_vercel_config (
    tenant_id         UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    -- Secret Manager / KMS reference to the Vercel access token — NEVER the raw token. Bounded so a
    -- fat-fingered raw token (which is long) is more likely to trip the check than land silently.
    access_token_ref  TEXT NOT NULL CHECK (length(access_token_ref) > 0 AND length(access_token_ref) < 512),
    -- Public Vercel identifiers the usage query is scoped to. Not secrets.
    team_id           TEXT,
    project_id        TEXT,
    -- Lets a tenant keep the row but pause the connector.
    enabled           BOOLEAN NOT NULL DEFAULT true,
    -- CTO-144 reconciliation gate: emit Vercel egress spans from THIS connector only when true.
    -- Default false — egress is owned by CTO-144's egress connector (see header).
    emit_egress       BOOLEAN NOT NULL DEFAULT false,
    last_run_at       TIMESTAMPTZ,
    last_status       TEXT CHECK (last_status IN ('success', 'partial', 'failed')),
    connected_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenant_vercel_config_tenant
    ON tenant_vercel_config(tenant_id);
