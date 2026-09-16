-- Committed billing usage per (tenant, period) (CTO-390).
--
-- WHY THIS TABLE EXISTS. Billing usage lived in process memory and nowhere else. The gateway's
-- lifespan built one `UsageRollup()`, `GET /v1/usage` read that object directly, and
-- `gateway/metering.py` is pure in-memory dicts with no storage behind them. Nothing persisted it
-- and nothing shared it.
--
-- That is not a rounding problem, it is three separate wrong answers:
--
--   * FRACTIONAL. Production runs more than one replica (deploy/aws/ecs/gateway.service.json has
--     desiredCount 2, both Helm charts default replicaCount 2, and Cloud Run scales 1 to 10). A
--     batch is metered by whichever replica accepted it, so each replica holds a disjoint slice of
--     the truth and every answer is a fraction of actual usage.
--   * UNSTABLE. Which fraction depends on which replica the load balancer routed the read to, so
--     two refreshes of the same dashboard legitimately disagree, with no way to tell which is less
--     wrong.
--   * AMNESIAC. A deploy, a crash or a scale-in reset the counters to zero. A tenant's invoice
--     evidence was destroyed by a routine rolling restart.
--
-- WHAT THIS TABLE IS, AND IS NOT. It is the COMMITTED figure for a period: the frozen, immutable
-- record of what a period finally counted (CTO-86 already requires closed periods to be immutable;
-- this is where that immutability becomes durable rather than a dict that dies with the worker).
--
-- It is NOT the live counter for the open period. Writing every ingest batch's contribution here
-- would put a Postgres write on the ingest hot path per batch, and the distinct-id semantics the
-- count needs (a trace seen twice counts once, across all replicas) are exactly what a row-per-batch
-- counter cannot express without reading back the whole set. The open period is instead computed on
-- read with `uniqExact` over `otel_spans` in ClickHouse, which already holds one row per span, is
-- already shared by every replica, and already does distinct-counting as its core competence. See
-- `gateway/usage_store.py` for how the two sources compose.
--
-- WHY THE COMMITTED ROW IS STILL NEEDED IF CLICKHOUSE CAN COUNT. Two reasons, both about time.
-- `otel_spans` carries a 90-day TTL (db/clickhouse/otel_spans.sql), so a period older than that
-- would silently start counting down toward zero, and a zero that used to be a real number is the
-- single worst failure this codebase recognises. And a closed period must not move at all: a late
-- backfill landing in a billed month would otherwise change an invoice that has already been sent.
-- Committing the figure freezes it against both.
--
-- HONEST UNDER UNCERTAINTY. The commitment columns are NULLABLE on purpose. `trace_commitment` is a
-- collision-resistant hash over the full set of distinct ids (gateway/metering.py), and ClickHouse
-- cannot produce it for an open period without materialising every id, which is unbounded. So an
-- open period reports its commitment as NULL, meaning "not computed", and only a committed period
-- carries a real one. A placeholder or an empty string here would be a hash that reconciles against
-- nothing while looking exactly like one that does.
--
-- The count columns are deliberately NOT nullable and are CHECKed non-negative: a committed row is
-- written only when a real figure is known. "Unknown" is represented by the ABSENCE of a row, never
-- by a zero, which is why the read path distinguishes "no committed row" from "committed as 0".

CREATE TABLE IF NOT EXISTS tenant_usage_periods (
    -- The tenant as the INGEST path spelled it, TEXT with no FK, matching
    -- ingest_batch_idempotency (0032) and tenant_ingest_cursors (0028). /v1/batches stores TenantId
    -- as the caller posted it and does not fold a name onto the UUID (see CLAUDE.md), so a usage row
    -- has to key on the same spelling the spans carry or it would count a different tenant's data.
    tenant_id        TEXT NOT NULL CHECK (length(tenant_id) > 0),
    -- UTC billing month, 'YYYY-MM', the same string gateway.metering.billing_period produces.
    period           TEXT NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    -- Distinct billable traces and distinct active feature tags for the period. Counts, never money:
    -- this table holds no currency amount at all, so the integer-micro-USD rule has nothing to say
    -- about it. Non-negative because a negative count is a bug, not a state.
    trace_count      BIGINT NOT NULL CHECK (trace_count >= 0),
    feature_count    BIGINT NOT NULL CHECK (feature_count >= 0),
    -- Tamper-evident commitments over the distinct id sets (CTO-84/85). NULL means "not computed",
    -- which is the honest value for a figure derived from an aggregate rather than from the id set.
    trace_commitment   TEXT,
    feature_commitment TEXT,
    -- The plan and ceilings in force when the period was committed, snapshotted rather than joined.
    -- A tenant that upgrades in March must not retroactively change what February was billed
    -- against, and a join to the live plan would do exactly that. NULL limit means unlimited, the
    -- same spelling gateway.metering.PlanLimit uses.
    plan             TEXT NOT NULL CHECK (length(plan) > 0 AND length(plan) <= 64),
    trace_limit      BIGINT CHECK (trace_limit IS NULL OR trace_limit >= 0),
    feature_limit    BIGINT CHECK (feature_limit IS NULL OR feature_limit >= 0),
    committed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period)
);

-- Supports "which periods are already committed for this tenant", the question the read path asks
-- before it decides whether to compute from ClickHouse. The primary key already leads on tenant_id,
-- so this is only about ordering the answer by period without a sort.
CREATE INDEX IF NOT EXISTS tenant_usage_periods_tenant_period_idx
    ON tenant_usage_periods (tenant_id, period DESC);
