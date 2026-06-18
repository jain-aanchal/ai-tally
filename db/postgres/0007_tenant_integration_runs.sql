-- Per-tenant third-party integration run status (CTO-117).
--
-- The /connectors page used to lean on a hardcoded ``mockActivity`` to populate per-tenant
-- "Connected / failing / not connected" status for the third-party integrations card
-- (Stripe, Segment, HubSpot, Pendo, …). This table is the real source: each row records the
-- outcome of the most recent worker / webhook cycle for one (tenant, connector) pair.
--
-- Workers (Stripe webhook handler today; Segment / HubSpot / Pendo pollers as they land) call
-- ``TenantIntegrationStore.record_run`` after each cycle. That's all this table tracks — what
-- happened last, with rolling totals for the dashboard.
--
-- Migration is additive: existing tenants have zero rows, which the dashboard reads as the
-- honest "Not connected" state. No backfill, no rewrite of mockActivity for first-render fallback.
--
-- ``last_run_error_message`` is PII-scrubbed at write time (see validation._FORBIDDEN_PII_KEYS /
-- _EMAIL_RE) — third-party errors sometimes echo customer emails verbatim and that must never
-- reach storage.

CREATE TABLE IF NOT EXISTS tenant_integration_runs (
    tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    connector_id           TEXT NOT NULL
                               CHECK (connector_id IN
                                   ('stripe','segment','hubspot','pendo','rudderstack')),
    last_run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_status        TEXT NOT NULL
                               CHECK (last_run_status IN ('success','partial','failed')),
    last_run_event_count   INTEGER NOT NULL DEFAULT 0
                               CHECK (last_run_event_count >= 0),
    last_run_error_message TEXT,
    total_events_24h       INTEGER NOT NULL DEFAULT 0
                               CHECK (total_events_24h >= 0),
    total_events_7d        INTEGER NOT NULL DEFAULT 0
                               CHECK (total_events_7d >= 0),
    PRIMARY KEY (tenant_id, connector_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_integration_runs_tenant
    ON tenant_integration_runs(tenant_id);
