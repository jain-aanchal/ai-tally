-- Per-tenant Stripe webhook + account config (CTO-110).
--
-- The Stripe webhook ingest endpoint (/v1/stripe/webhook on the gateway) needs the per-tenant
-- signing secret to verify each delivery's Stripe-Signature header. Stripe's SDK requires the
-- ORIGINAL secret to recompute the expected HMAC — we can't store a hash and compare, so the
-- secret is persisted reversibly.
--
-- Storage tradeoff (documented for the next pair of eyes):
--   * In production, ``webhook_secret`` should be replaced with a KMS reference + envelope
--     encryption (or stored in Vault / AWS Secrets Manager and referenced by ARN here), matching
--     how ``tenants.hash_salt_kek_ref`` works (see 0001_control_plane.sql).
--   * For local dev / self-hosted, we store the raw secret in this column to keep the loop
--     runnable without a KMS dependency. The CHECK constraint below pins the prefix Stripe uses
--     so a clearly-wrong value (a publishable key, an API key) gets rejected at insert time.
-- The audit table records every connect / rotate / disconnect so a tenant admin can see the
-- last time a secret changed (and who did it) even though we never expose the secret again.

CREATE TABLE IF NOT EXISTS tenant_stripe_config (
    tenant_id            UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    stripe_account_id    TEXT,
    webhook_secret       TEXT NOT NULL
                             CHECK (webhook_secret LIKE 'whsec_%' AND length(webhook_secret) < 512),
    connected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    disconnected_at      TIMESTAMPTZ,
    notes                TEXT
);

-- Audit trail: append-only. ``change_id`` is the idempotency key so retried API calls don't
-- produce duplicate rows.
CREATE TABLE IF NOT EXISTS tenant_stripe_changes (
    change_id     UUID PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    change_kind   TEXT NOT NULL CHECK (change_kind IN ('connected', 'rotated', 'disconnected')),
    actor         TEXT,
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenant_stripe_changes_tenant
    ON tenant_stripe_changes(tenant_id, at DESC);
