-- Per-tenant opt-in for replay sampling + projection (CTO-113).
--
-- Workflows 2 (Compare) and 5 (Estimate) project candidate-model cost/latency by replaying real
-- prompts against alternative providers. Capturing those prompts is a non-trivial trust ask, so
-- this is **opt-in per tenant**. Default `enabled=false`; existing tenants don't suddenly start
-- sampling.
--
-- `sample_rate` is the fraction of ingested spans we capture (default 5%). `retention_days` caps
-- how long the captured payloads live in object storage. `daily_budget_usd` is the hard ceiling
-- on the replay executor's spend per tenant per day — a bug in replay must never burn $10k.

CREATE TABLE IF NOT EXISTS tenant_replay_config (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    enabled          BOOLEAN NOT NULL DEFAULT false,
    sample_rate      REAL NOT NULL DEFAULT 0.05
                         CHECK (sample_rate >= 0.0 AND sample_rate <= 1.0),
    retention_days   INTEGER NOT NULL DEFAULT 30
                         CHECK (retention_days > 0),
    daily_budget_usd NUMERIC(10, 2) NOT NULL DEFAULT 5.00
                         CHECK (daily_budget_usd >= 0),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
