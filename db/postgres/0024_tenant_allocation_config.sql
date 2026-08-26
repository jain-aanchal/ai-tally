-- Per-tenant shared-cost allocation rule (CTO-193, plan C2).
--
-- WHY this row exists. The cost-per-customer tab reports direct spend (LLM, tools, vector,
-- embeddings) per account and, since CTO-192, an ALLOCATED share of the tenant's compute and
-- egress on top of it. Compute and egress arrive from the cloud billing connectors as one
-- synthetic span per provider per day at tenant level with no account on them, so splitting them
-- per customer means choosing a rule. On current data that shared half is roughly 47 percent of
-- the bill, so the rule is not a detail: it decides about half of every number on the page.
--
-- A rule that decides half the page cannot be a constant compiled into the dashboard. Two tenants
-- disagree about it honestly (a tenant whose infra scales with model calls wants pro rata; a
-- tenant running a fixed cluster per customer wants an even split), and a reader who cannot see
-- which rule produced their number has no way to argue with it. So it lives here, per tenant, and
-- the page names the rule in force beside the column it produced.
--
-- Allowed values, mirroring `AllocationRule` in web/lib/allocation.ts exactly:
--   * pro_rata_direct: each account's share of shared infra is proportional to its direct spend.
--     THE DEFAULT. Assumes infra broadly scales with model usage, which is the least wrong
--     assumption available without a per-account infra driver metered (that is the "pro rata on a
--     driver" row of Decision 3 in docs/cost-per-customer-scope.md, and it needs telemetry that
--     does not exist yet).
--   * even_split: shared cost divided equally across participants. Weaker, since it flatters heavy
--     accounts and punishes small ones. Offered because a tenant who provisions per customer
--     rather than per request is genuinely better described by it.
--
-- INVARIANT: absence of a row is a fully supported state and means the default. There is no
-- backfill and no placeholder row. Every tenant on this system today has no row, must keep
-- working, and must get `pro_rata_direct`. The CHECK constraint is the second line of defence
-- behind the API validation: an unknown rule string reaching the dashboard would either crash the
-- page or silently fall back, and a silent fallback to a DIFFERENT rule than the one stored is the
-- one failure mode that would make the named rule on screen a lie.
--
-- Reads and writes both go through GET/POST /v1/tenant/allocation-config. The web app never
-- touches Postgres directly, same rule as tenant_account_labels (0023) and
-- tenant_unit_economics_config (0019).
--
-- WHY there IS an audit companion here, unlike tenant_account_labels (0023). That table skips one
-- because an audit row would preserve a deleted customer NAME, defeating its escape hatch. Nothing
-- of the kind applies here: the payload is one enum value from a fixed set of two, and it carries
-- no customer data at all. What it does carry is the answer to "why did every allocated figure on
-- this page move overnight", which is precisely the question a reader asks when the rule changes
-- underneath them. Same shape as tenant_unit_economics_config_changes: one row per upsert, keyed
-- by a client-supplied change_id so a retried request is idempotent on both writes.

CREATE TABLE IF NOT EXISTS tenant_allocation_config (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    allocation_rule  TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT,
    -- Kept in lockstep with ALLOCATION_RULES in web/lib/allocation.ts and ALLOCATION_RULES in
    -- gateway/tenant_allocation.py. Adding a rule means touching all three, deliberately: a rule
    -- the engine cannot apply must not be storable.
    CONSTRAINT tenant_allocation_config_rule_known
        CHECK (allocation_rule IN ('pro_rata_direct', 'even_split'))
);

CREATE TABLE IF NOT EXISTS tenant_allocation_config_changes (
    change_id        UUID NOT NULL,
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor            TEXT,
    before           JSONB,
    after            JSONB,
    changed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, change_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_allocation_config_changes_tenant
    ON tenant_allocation_config_changes(tenant_id, changed_at DESC);
