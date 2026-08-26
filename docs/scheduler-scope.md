# Scope: the scheduler

**Status: proposal.** Periodic per-tenant job execution. Nothing in this repo schedules anything, and five features are blocked on that.

## The problem, measured

This is not a theoretical gap. Each of these shipped, or is scoped, with working code that nothing ever calls:

| Blocked | Evidence |
| --- | --- |
| Cloud cost connectors | `ComputeCostConnector` / `EgressCostConnector` / `VercelCostConnector` all have `run()` and `run_backfill()`. Only `scripts/backfill_*.py` invoke them, by hand. A tenant who connects AWS through the dashboard gets config that nothing acts on. |
| Attribution stitcher | CTO-200. `attribution_records` has 0 rows for every tenant because no runner exists, so `/features` has always shown honest nulls for value, payback and attribution rate. |
| Third-party ingest | `IngestWorker.run_cycle(tenant_id)` exists for Segment, HubSpot and Pendo. Nothing calls it. |
| Cost alerts | `docs/cost-alerts-scope.md`. An alert evaluator is useless without a timer. |
| Waste detection | `docs/waste-detection-scope.md`. Continuous scanning needs one. |

Reconciliation (`run_reconciliation`) is in the same position.

So the first job wired up is not new value, it is **switching on value that already merged**.

## What exists to build on

`AsyncIngestBuffer` in `gateway/ingest_buffer.py` is the precedent and should be followed rather than re-invented: `asyncio.create_task` started from the FastAPI `lifespan`, a `while not self._stop.is_set()` loop, `start()` / `stop()`, and graceful cancellation on shutdown. It is already opt-in behind `settings.ingest_buffered`.

Run recording also exists and must be reused, not replaced. Connector config rows carry `last_run_at` / `last_status` and the connectors already call `record_run` themselves. `tenant_integration_runs` (0007) and `reconciliation_runs` (0008) are the same shape. The scheduler's job is to *call* those paths on a cadence, not to invent a second source of truth for "did it run".

## Decision 1: where it runs

**In-process in the gateway**, started from `lifespan`, opt-in behind a settings flag defaulting to off.

The alternative is a separate worker process or container. That is the textbook answer and the wrong one here for now: every store the jobs need is already constructed on `app.state`, the jobs are daily rather than high-frequency, and a second deployment unit is a real operational cost for a product that is still self-hosted by hand. The seam to extract one later is the job registry, which should not assume it is running inside a web process.

The cost of this choice is that jobs share a process with the API. A slow job must not block request handling, so every job runs off the event loop thread, and no job may hold the loop.

## Decision 2: due-calculation, not sleeping

A daily job must NOT be `await asyncio.sleep(86400)`. That drifts, and it loses its place on every restart, so a gateway that redeploys daily never runs a daily job at all.

Instead: a **tick loop** every few minutes asks each registered job "are you due for this tenant?", answered from the last successful run recorded in Postgres. Restart-safe, drift-free, and catches up a missed window on the next tick rather than skipping it.

Catch-up must be **bounded**: a tenant whose connector has been broken for a month should get one run, not thirty queued.

## Decision 3: multi-replica safety

Two gateway replicas would otherwise run every job twice, which for cost connectors means double-counted spend. Since compute/egress spans are keyed on `synthetic_span_id(tenant, provider, operation, day)` the emitter's `span_exists` guard would catch that particular case, but nothing else is protected and relying on a downstream dedupe is not a design.

**Postgres advisory locks** (`pg_try_advisory_lock`) keyed on job plus tenant. A replica that cannot take the lock skips that tenant this tick. No new infrastructure, no leader election, and the lock dies with the connection so a crashed replica does not wedge a job forever.

## Decision 4: failure isolation

One tenant's failure must never stop another tenant's job, and one job's failure must never stop the tick loop. Every job invocation is wrapped, records `failed` with a scrubbed error, and the loop continues.

Backoff on repeated failure so a permanently broken connector is not retried every tick forever. The existing posture applies: a failed run records `failed` and emits nothing. Never a guessed number.

## What it is not

- Not a general task queue. No user-submitted jobs, no fan-out, no chaining.
- Not sub-minute. The finest useful cadence here is minutes; everything real is hourly or daily.
- Not a replacement for the backfill scripts, which stay for one-off catch-up.

## Phasing

1. **Run-history table and scheduler core**: registry, due-calculation, tick loop, lifespan wiring, settings flag. No jobs registered.
2. **Advisory locking**, so it is safe to run more than one gateway.
3. **The cloud cost connectors job.** The one that switches on already-merged value.
4. **The reconciler and the third-party ingest workers.**
5. **A dashboard surface**: what ran, when, what failed, what is overdue. Without this a scheduler is invisible until something is wrong.

## Open questions

1. Does a tenant get to configure cadence, or is it per job? Per job is simpler and probably right for v1.
2. What happens to a job whose tenant was deleted mid-run? The FK cascades; the job must tolerate the row vanishing.
3. Should the tick loop run on every replica with locking (simple, wasteful) or elect a leader (efficient, more machinery)? Locking for v1.
4. How long is run history kept? It is per tenant per job per day, so it grows slowly, but it needs a retention answer eventually.

## Risks

**A scheduler that silently stops is worse than no scheduler**, because the dashboard keeps rendering yesterday's numbers as though they were current. Phase 5 is not polish: the "last successful run" figure is what makes the whole thing trustworthy, and every surface fed by a scheduled job should be able to say how fresh it is.

**Switching on the cost connectors will change customers' numbers.** Compute and egress are roughly 46 percent of spend on the demo tenant. A tenant who connected AWS weeks ago and saw nothing will suddenly see their bill nearly double. That is correct, and it will still look like a bug unless it is announced.
