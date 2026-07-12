-- Reconciler pipeline run log (CTO-139).
--
-- The /features "Attribution diagnostics" card shows three tenant-wide signals — late-arrival
-- event count, median late-arrival lag, and "reconciler last ran N minutes ago". Those were
-- honestly zero before this ticket because no reconciler ever ran. This table is the real source:
-- each row records the outcome of one reconciliation pass for a tenant.
--
-- A pass scans recent business_events against their matched span timestamps, counts events that
-- arrived "late" (event OccurredAt more than 1h after the matched span), and stamps the lag
-- distribution. ``ReconciliationStore.record_run`` writes a row after each pass; the dashboard
-- reads the latest via GET /v1/tenant/reconciliation/status.
--
-- Migration is additive: existing tenants have zero rows, which the dashboard reads as the honest
-- "no reconciler run yet" state (the web fn returns null and the route falls back to its mock).
--
-- ``tenant_id`` is plain TEXT here (not a UUID FK to tenants) because the reconciler keys off the
-- same TenantId string the telemetry path uses end-to-end; it runs per-tenant off ClickHouse data
-- and never needs the control-plane tenant row.

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    tenant_id          TEXT NOT NULL,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    events_late        INTEGER NOT NULL DEFAULT 0
                           CHECK (events_late >= 0),
    lag_seconds_median INTEGER NOT NULL DEFAULT 0
                           CHECK (lag_seconds_median >= 0),
    lag_seconds_p95    INTEGER NOT NULL DEFAULT 0
                           CHECK (lag_seconds_p95 >= 0),
    status             TEXT NOT NULL
                           CHECK (status IN ('ok','partial','failed'))
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_tenant_finished
    ON reconciliation_runs(tenant_id, finished_at DESC);
