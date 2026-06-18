-- Per-tenant opt-in for cross-provider eval (CTO-114).
--
-- The eval harness re-uses CTO-113's replay corpus and asks an impartial LLM judge which of
-- (current_response, candidate_response) better follows the original instruction. Judge calls
-- are *pricier* than replay calls (a frontier judge model rated for nuance, not cost), so the
-- daily budget defaults higher than replay's ($10 vs $5) but is still hard-capped.
--
-- Off by default — eval re-issues prompts to a frontier model, which is a separate trust ask
-- from "we already capture samples". The /compare page falls back to "—" cells when this is
-- off so we never fabricate a quality number.

CREATE TABLE IF NOT EXISTS tenant_eval_config (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    enabled          BOOLEAN NOT NULL DEFAULT false,
    -- Judge model identifier; the gateway looks this up in the price catalog at call time.
    -- Default is claude-opus-4-8 — highest-capability judge in the catalog. Override per tenant
    -- if a customer requires a specific judge for compliance reasons (or wants to rotate to
    -- mitigate judge-self-bias against same-family candidates).
    judge_model      TEXT NOT NULL DEFAULT 'claude-opus-4-8',
    daily_budget_usd NUMERIC(10, 2) NOT NULL DEFAULT 10.00
                         CHECK (daily_budget_usd >= 0),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
