-- Scheduler run history (CTO-213, S1).
--
-- WHY this table exists. The scheduler does not remember when it last ran anything; Postgres does.
-- That is the whole design. A daily job implemented as ``await asyncio.sleep(86400)`` drifts, and
-- it loses its place on every restart, so a gateway that redeploys daily would never run a daily
-- job at all. Instead a tick loop wakes every few minutes and asks each registered job "are you due
-- for this tenant?", and the answer is a query against this table. Restart-safe and drift-free,
-- because the state that matters is on disk rather than in a sleeping coroutine.
--
-- One row per invocation, per job, per tenant. ``status`` is the honest outcome:
--
--   success  the job ran and did its work.
--   skipped  the job ran and decided there was nothing to do (a tenant with no config for it).
--            Not a failure and not a success: it settles the cadence window so an unconfigured
--            tenant is not retried every tick, but it never claims work happened.
--   failed   the job raised. ``error_message`` carries the scrubbed reason and NOTHING was
--            produced. The repo rule holds here: a failed run records the failure and emits no
--            number, never a guessed one.
--
-- ``error_message`` is PII-scrubbed at write time by gateway.tenant_integrations.scrub_error_message
-- before the parameter is bound, the same as tenant_integration_runs (0007) does. Job errors bubble
-- up from third-party SDKs that sometimes echo customer emails verbatim, and that must never reach
-- storage.
--
-- WHY tenant_id is TEXT and not a UUID FK to tenants, unlike 0007. Two reasons. This is an
-- operational log that has to survive the tenant row vanishing mid-run (a cascade would delete the
-- record of the run that was in flight when the tenant was deleted, which is exactly the history an
-- operator wants at that moment). And the jobs this schedules key off different tenant spellings:
-- the cost connectors take the tenants.id UUID, the reconciler takes the TenantId string used
-- end-to-end in telemetry. TEXT holds both; reconciliation_runs (0008) made the same call.
--
-- Migration is additive. Existing deployments have zero rows, which reads as "nothing has ever been
-- scheduled" and is the true state of every deployment today: the scheduler ships opt-in behind
-- TALLY_SCHEDULER_ENABLED, defaulting to off, and CTO-213 registers no jobs at all.

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name      TEXT NOT NULL CHECK (length(job_name) > 0 AND length(job_name) < 128),
    tenant_id     TEXT NOT NULL CHECK (length(tenant_id) > 0),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL CHECK (status IN ('success', 'skipped', 'failed')),
    error_message TEXT,
    -- A failed run must not carry a result and a successful one must not carry an error. Cheap
    -- guard against a caller stamping a success with the last exception still in hand.
    CONSTRAINT scheduler_runs_error_only_on_failure
        CHECK (error_message IS NULL OR status = 'failed'),
    CONSTRAINT scheduler_runs_finished_after_started
        CHECK (finished_at >= started_at)
);

-- The tick loop asks "when did this job last settle for this tenant" for every (job, tenant) pair
-- on every tick, which is the hottest query in the feature by a wide margin. This index is what
-- makes it an index-only backwards scan rather than a growing sequential one.
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job_tenant_finished
    ON scheduler_runs(job_name, tenant_id, finished_at DESC);

-- Same question narrowed to "last SUCCESS", which is what the freshness surfaces (phase 5 of
-- docs/scheduler-scope.md) report and what backoff counts failures since. Partial, so it stays
-- small even when a broken job fills the table with failures.
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job_tenant_success
    ON scheduler_runs(job_name, tenant_id, finished_at DESC)
    WHERE status = 'success';
