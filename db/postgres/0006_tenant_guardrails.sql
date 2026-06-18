-- Per-tenant guardrail control-plane (CTO-116).
--
-- Replaces the mock guardrail rules with a real registry. Each row is one rule scoped to a
-- tenant; `kind` is the enforcement category, `params` is the kind-specific config (jsonb),
-- `state` is the rollout stage. Companion table `tenant_guardrail_changes` is an audit trail —
-- every upsert appends one row keyed by client-supplied `change_id` for idempotent replay.
--
-- The SDK polls the gateway for the active rule set and enforces matching rules in-process; the
-- web app reads through the gateway, never Postgres directly. Same shape as CTO-107.

CREATE TABLE IF NOT EXISTS tenant_guardrails (
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rule_id     TEXT NOT NULL,
    kind        TEXT NOT NULL
                    CHECK (kind IN ('pii_gate','cost_cap','loop_limit','model_deprecation')),
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    state       TEXT NOT NULL DEFAULT 'shadow'
                    CHECK (state IN ('enabled','shadow','disabled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT,
    notes       TEXT,
    PRIMARY KEY (tenant_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_guardrails_tenant ON tenant_guardrails(tenant_id);

CREATE TABLE IF NOT EXISTS tenant_guardrail_changes (
    change_id   UUID NOT NULL,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rule_id     TEXT NOT NULL,
    actor       TEXT,
    before      JSONB,
    after       JSONB,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, change_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_guardrail_changes_tenant_rule
    ON tenant_guardrail_changes(tenant_id, rule_id, changed_at DESC);
