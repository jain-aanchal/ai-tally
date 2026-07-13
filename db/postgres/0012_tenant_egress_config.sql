-- Per-tenant egress (bandwidth-out) billing connector config (CTO-144).
--
-- Sibling of 0011_tenant_compute_config: the Egress column on /cost is $0 on every live deployment
-- because no bandwidth-billing data reaches otel_spans. The daily egress connector
-- (gateway.connectors.egress) fixes that: it pulls a tenant's bandwidth-out spend from Vercel,
-- Cloudflare, and/or AWS and lands it as synthetic `egress`-layer spans. This table is the
-- per-tenant, per-provider control-plane row that connector reads.
--
-- MULTIPLE providers per tenant (unlike compute's one): a tenant can serve traffic through Vercel
-- AND Cloudflare AND AWS at once, so the PK is composite on (tenant_id, egress_provider). The
-- connector runs once per row and keys each synthetic span on the provider, so the three sum on
-- /cost with NO double-counting.
--
-- Secrets by REFERENCE only (same hard rule as 0011): ``credentials_ref`` is a Secret Manager / KMS /
-- ARN pointer (or 'aws-default-chain') — never raw keys. ``resource_id`` is the PUBLIC per-provider
-- identifier the fetch is scoped to (Cloudflare zone id / Vercel team id / AWS account id), not a
-- secret.
--
-- ``usd_per_gb`` prices Cloudflare's bytes-out (its analytics report bytes, not dollars). It is
-- NULL for Vercel/AWS (which report USD directly) and REQUIRED for Cloudflare — a Cloudflare row
-- without it makes the connector fail soft (a 'failed' run, no span) rather than guess a price.
--
-- Run status lives on the same row (last_run_at / last_status), mirroring 0011. A failed fetch
-- stamps 'failed' and emits NO span (honest-under-uncertainty — never a guessed number).
--
-- Migration is additive: existing tenants have zero rows, which the connector treats as
-- "egress connector not configured" and skips.

CREATE TABLE IF NOT EXISTS tenant_egress_config (
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    egress_provider  TEXT NOT NULL CHECK (egress_provider IN ('vercel', 'cloudflare', 'aws')),
    -- Secret Manager / KMS / ARN reference — NEVER raw credentials. Bounded so a fat-fingered raw
    -- key (which is long) is more likely to trip the check than land silently.
    credentials_ref  TEXT NOT NULL CHECK (length(credentials_ref) > 0 AND length(credentials_ref) < 512),
    -- Public per-provider identifier the fetch is scoped to (cloudflare zone / vercel team / aws
    -- account). Not a secret.
    resource_id      TEXT,
    -- USD per GiB used to price Cloudflare bytes-out. NULL for vercel/aws; required for cloudflare.
    usd_per_gb       NUMERIC(12, 6) CHECK (usd_per_gb IS NULL OR usd_per_gb >= 0),
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT CHECK (last_status IN ('success', 'partial', 'failed')),
    connected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes            TEXT,
    -- Composite PK: multiple egress providers per tenant, one row each.
    PRIMARY KEY (tenant_id, egress_provider)
);

CREATE INDEX IF NOT EXISTS idx_tenant_egress_config_tenant
    ON tenant_egress_config(tenant_id);
