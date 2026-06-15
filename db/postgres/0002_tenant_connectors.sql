-- Per-tenant declared cost-layer connectors (CTO-107).
--
-- The dashboard "Partial data" banner used to fire whenever any cost layer reported zero, which
-- made it permanent on every demo (only the LLM connector is wired today) and trained users to
-- ignore it. We fix that by declaring which connectors a tenant has actually enabled: the banner
-- now fires only for *declared* layers that go silent.
--
-- One row per (tenant_id, layer). A NULL disabled_at means the connector is currently enabled;
-- a non-NULL disabled_at means the tenant turned it off (kept as a tombstone for audit). The row
-- itself is the audit trail — enabled_at / disabled_at / notes — so toggles never delete history.

CREATE TABLE IF NOT EXISTS tenant_connectors (
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    layer        TEXT NOT NULL
                     CHECK (layer IN ('llm','vector','tools','compute','embeddings','egress')),
    enabled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at  TIMESTAMPTZ,
    notes        TEXT,
    PRIMARY KEY (tenant_id, layer)
);

CREATE INDEX IF NOT EXISTS idx_tenant_connectors_tenant ON tenant_connectors(tenant_id);
