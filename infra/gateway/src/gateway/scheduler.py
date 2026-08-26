# SPDX-License-Identifier: Apache-2.0
"""Periodic per-tenant job execution: registry, due-calculation, tick loop (CTO-213, S1).

WHY this exists. Nothing in this repo schedules anything, and a pile of working, tested code is
invoked by nobody as a result: the compute / egress / Vercel cost connectors have ``run()`` and are
called only by ``scripts/backfill_*.py`` by hand, ``IngestWorker.run_cycle`` exists for Segment,
HubSpot and Pendo and nothing calls it, ``run_reconciliation`` is in the same position. So the first
job wired up here is not new value, it is switching on value that already merged. See
``docs/scheduler-scope.md`` for the full argument.

THIS MODULE IS THE ENGINE ONLY. It registers no jobs. Wiring the cost connectors is CTO-215 and the
ingest workers CTO-216, and until one of those lands an enabled scheduler ticks over an empty
registry and does nothing. That is deliberate: the engine lands and gets exercised on its own.

The central design point: a daily job is NOT ``await asyncio.sleep(86400)``.
----------------------------------------------------------------------------
That spelling drifts (every cycle's own runtime is added to the next interval), and worse, it keeps
its place in memory, so it loses it on every restart. A gateway that redeploys once a day would
never run a daily job at all: each deploy restarts the sleep, and the sleep never finishes. The bug
would be invisible, because a scheduler that silently does nothing looks exactly like a scheduler
with nothing to do.

Instead: a tick loop wakes every few minutes and asks each registered job "are you due for THIS
tenant?", and the answer comes from the run history in Postgres (``scheduler_runs``, migration
0027). Nothing is held in memory across a tick. A restart mid-window resumes exactly where it left
off, a missed window is caught on the next tick rather than skipped, and the cadence cannot drift
because it is measured from a recorded timestamp rather than accumulated from sleeps.

Catch-up is BOUNDED by construction. :func:`is_due` returns a boolean, never a count of missed
windows, and a tick runs each (job, tenant) pair at most once. A tenant whose connector has been
broken for a month therefore gets ONE run on the next tick, not thirty queued. If a job ever needs
to reprocess the days it missed, that belongs inside the job (the connectors already have
``run_backfill``) where the range is explicit and bounded, not in the scheduler's clock.

Failure isolation. One tenant's failure must never stop another tenant, and one job's failure must
never stop the loop. Every invocation is wrapped: a raise records ``failed`` with a scrubbed error
and the loop moves on to the next tenant. Repeated failure earns exponential backoff (see
:func:`backoff_delay_s`) so a permanently broken job is not retried every tick forever, which would
otherwise mean hammering a dead third-party API every few minutes indefinitely and burying the run
history under identical failures.

Nothing blocks the event loop. The jobs this will eventually call are synchronous and do blocking
Postgres and HTTP work, and the scheduler runs in-process with the API, so every job invocation and
every store query goes through :func:`asyncio.to_thread`. A slow job delays the next tick; it never
delays a request. There is deliberately no per-job timeout: ``asyncio.wait_for`` around a
``to_thread`` abandons the coroutine but cannot kill the thread, so it would report a lie ("timed
out", while the job keeps running and writing) rather than enforce anything.

The registry does NOT assume it is running inside a web process. It takes plain callables and a
tenant provider and knows nothing about FastAPI or ``app.state``. That is the seam
``docs/scheduler-scope.md`` commits to for extracting a worker container later: the extraction
becomes "construct the registry somewhere else", not a rewrite.

Shape follows :class:`gateway.ingest_buffer.AsyncIngestBuffer`, the existing precedent for a
background task here: ``asyncio.create_task``, ``while not self._stop.is_set()``, ``start()`` /
``stop()``, graceful cancellation from the FastAPI ``lifespan``, and opt-in behind a settings flag
(``settings.scheduler_enabled``, default off, matching ``ingest_buffered`` and the export flags).
With the flag off, behaviour is byte-identical to today.

Not covered here, by design: multi-replica safety. Two replicas with the flag on would run every job
twice. Advisory locking (``pg_try_advisory_lock`` keyed on job plus tenant) is phase 2 of the scope
doc; until it lands, enable the scheduler on exactly one gateway replica.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

import psycopg

from gateway.config import Settings
from gateway.tenant_integrations import scrub_error_message

logger = logging.getLogger("tally.gateway.scheduler")

#: Outcome of one invocation. Mirrors the CHECK constraint in ``0027_scheduler_runs.sql``.
#:
#: ``skipped`` is not a soft failure. It is a job saying "there is nothing to do for this tenant",
#: which is the normal state for a tenant that has not configured the thing the job runs (most
#: tenants, for most jobs). It settles the cadence window so the job is not re-asked every tick, and
#: it never claims work happened.
RunStatus = Literal["success", "skipped", "failed"]

#: A job body: takes a tenant id, does its work synchronously, returns nothing. Raising means the
#: run failed. Raising :class:`JobSkipped` means there was nothing to do.
JobFn = Callable[[str], None]

#: How the scheduler learns which tenants exist. A plain callable so the registry stays free of any
#: assumption about where it runs; :class:`PostgresTenantProvider` is the default implementation.
TenantProvider = Callable[[], Sequence[str]]

# Tick cadence floor. The finest useful cadence in this product is minutes (everything real is
# hourly or daily), and a sub-second tick against Postgres would be pure waste.
_MIN_TICK_INTERVAL_S = 1.0

# Backoff defaults. The first failure retries on the next tick as normal (a transient 503 should not
# cost a whole window), and only repeated failure stretches the gap: 2 failures -> 5 min, 3 -> 10,
# 4 -> 20, capped at 6h so a job broken for a week still retries a few times a day and recovers on
# its own once whatever broke is fixed.
_DEFAULT_BACKOFF_BASE_S = 300.0
_DEFAULT_BACKOFF_CAP_S = 6 * 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobSkipped(Exception):
    """Raised by a job body to record ``skipped`` rather than ``success`` for this tenant.

    The intended use is "this tenant has not configured me": the cost connectors already treat a
    missing config row that way. It settles the cadence window without claiming work was done.
    """


@dataclass(frozen=True, slots=True)
class Job:
    """A name, a cadence, and a callable taking a tenant id. That is the whole abstraction.

    ``name`` is the identity persisted in ``scheduler_runs.job_name`` and is therefore a stable
    contract: renaming a job orphans its history and makes it look like it has never run, which
    would trigger an immediate run for every tenant.
    """

    name: str
    interval_s: float
    fn: JobFn

    def __post_init__(self) -> None:
        if not self.name or len(self.name) >= 128:
            raise ValueError("job name must be non-empty and shorter than 128 chars")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be > 0")


@dataclass(frozen=True, slots=True)
class JobState:
    """What the run history says about one (job, tenant) pair. Everything :func:`is_due` needs.

    ``last_settled_at`` is the cadence anchor: the most recent run that either succeeded or was
    skipped. ``last_success_at`` is narrower and is what a freshness surface should quote to a user,
    because "last skipped 10 minutes ago" is not "we have current data". They are kept separate so
    the scheduler can be efficient about unconfigured tenants without the dashboard ever being able
    to mistake a skip for a success.
    """

    last_success_at: datetime | None = None
    last_settled_at: datetime | None = None
    last_attempt_at: datetime | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one pass over the registry did. Returned for tests and logged when anything ran."""

    considered: int = 0  # (job, tenant) pairs examined
    ran: int = 0  # job bodies actually invoked
    succeeded: int = 0
    skipped_by_job: int = 0  # invoked, raised JobSkipped
    failed: int = 0
    not_due: int = 0  # inside the cadence window or serving backoff, so never invoked


def backoff_delay_s(
    consecutive_failures: int,
    *,
    base_s: float = _DEFAULT_BACKOFF_BASE_S,
    cap_s: float = _DEFAULT_BACKOFF_CAP_S,
) -> float:
    """Minimum gap after ``consecutive_failures`` failures before the job may be attempted again.

    Zero for a first failure: the next tick retries as usual, because most failures are a transient
    upstream blip and making the customer wait an extra window for one 503 is worse than one wasted
    retry. From the second failure on it doubles, capped, so a permanently broken job settles into a
    few attempts a day instead of one every tick forever.
    """
    if consecutive_failures <= 1:
        return 0.0
    return min(cap_s, base_s * (2.0 ** (consecutive_failures - 2)))


def is_due(
    job: Job,
    state: JobState,
    now: datetime,
    *,
    backoff_base_s: float = _DEFAULT_BACKOFF_BASE_S,
    backoff_cap_s: float = _DEFAULT_BACKOFF_CAP_S,
) -> bool:
    """Is ``job`` due for the tenant that ``state`` describes, as of ``now``? Pure, hence testable.

    Three questions, in order:

    1. Is the job serving backoff after repeated failures? Measured from the last ATTEMPT, so
       backoff holds regardless of how long ago the last success was.
    2. Has it ever settled (succeeded or skipped)? If not it is due immediately: that is a
       newly-registered job, or a brand-new tenant.
    3. Has a full cadence window passed since it last settled?

    Note what is absent: any notion of how MANY windows were missed. That is what bounds catch-up.
    """
    if state.consecutive_failures > 0 and state.last_attempt_at is not None:
        delay = backoff_delay_s(
            state.consecutive_failures, base_s=backoff_base_s, cap_s=backoff_cap_s
        )
        if delay > 0 and (now - state.last_attempt_at).total_seconds() < delay:
            return False
    if state.last_settled_at is None:
        return True
    return (now - state.last_settled_at).total_seconds() >= job.interval_s


class JobRegistry:
    """The set of registered jobs. A plain container: no I/O, no event loop, no web framework.

    Deliberately dumb, because it is the seam. Extracting a worker container later means building
    one of these in a different process; nothing in here would change.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def register(self, name: str, interval_s: float, fn: JobFn) -> Job:
        """Add a job. A duplicate name is a programming error, not a silent overwrite."""
        if name in self._jobs:
            raise ValueError(f"job '{name}' is already registered")
        job = Job(name=name, interval_s=float(interval_s), fn=fn)
        self._jobs[name] = job
        return job

    def get(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def jobs(self) -> tuple[Job, ...]:
        """Registration order, so a tick is deterministic and reproducible in tests."""
        return tuple(self._jobs.values())

    def __len__(self) -> int:
        return len(self._jobs)

    def __iter__(self) -> Iterator[Job]:
        return iter(self._jobs.values())


class RunStore(Protocol):
    """What the tick loop needs from run history. Synchronous; the loop calls it off-thread."""

    def get_state(self, job_name: str, tenant_id: str) -> JobState: ...

    def record_run(
        self,
        job_name: str,
        tenant_id: str,
        status: RunStatus,
        *,
        started_at: datetime,
        finished_at: datetime,
        error_message: str | None = None,
    ) -> None: ...


class SchedulerRunStore:
    """Postgres-backed run history over ``scheduler_runs`` (migration 0027).

    Mirrors :class:`gateway.tenant_integrations.TenantIntegrationStore`: a connection per call,
    tenant-scoped SQL, and error strings scrubbed before the parameter is bound.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get_state(self, job_name: str, tenant_id: str) -> JobState:
        """Everything :func:`is_due` needs for one (job, tenant) pair, in one round trip.

        The failure count is "failures since the last settled run", which is what makes backoff
        reset by itself: one success (or one skip) makes the count zero again without anybody
        clearing a counter, so there is no in-memory state to lose on restart or to drift out of
        sync with the history.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH agg AS (
                    SELECT max(finished_at) FILTER (WHERE status = 'success')
                               AS success_at,
                           max(finished_at) FILTER (WHERE status IN ('success', 'skipped'))
                               AS settled_at,
                           max(finished_at)
                               AS attempt_at
                    FROM scheduler_runs
                    WHERE job_name = %(job)s AND tenant_id = %(tenant)s
                )
                SELECT agg.success_at,
                       agg.settled_at,
                       agg.attempt_at,
                       (SELECT count(*)
                          FROM scheduler_runs r
                         WHERE r.job_name = %(job)s
                           AND r.tenant_id = %(tenant)s
                           AND r.status = 'failed'
                           AND r.finished_at
                               > coalesce(agg.settled_at, '-infinity'::timestamptz))
                FROM agg
                """,
                {"job": job_name, "tenant": tenant_id},
            )
            row = cur.fetchone()
        if row is None:  # pragma: no cover - the aggregate always yields exactly one row
            return JobState()
        return JobState(
            last_success_at=row[0],
            last_settled_at=row[1],
            last_attempt_at=row[2],
            consecutive_failures=int(row[3] or 0),
        )

    def record_run(
        self,
        job_name: str,
        tenant_id: str,
        status: RunStatus,
        *,
        started_at: datetime,
        finished_at: datetime,
        error_message: str | None = None,
    ) -> None:
        """Append one immutable row. Only a ``failed`` run may carry an error, per the CHECK."""
        scrubbed = scrub_error_message(error_message) if status == "failed" else None
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduler_runs
                    (job_name, tenant_id, started_at, finished_at, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (job_name, tenant_id, started_at, finished_at, status, scrubbed),
            )
            conn.commit()


class PostgresTenantProvider:
    """Every tenant in the control plane, which is the default population for every job.

    A job that only applies to configured tenants raises :class:`JobSkipped` for the rest, rather
    than the scheduler trying to guess who is in scope: the job is the only thing that knows what
    "configured" means for it.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def __call__(self) -> Sequence[str]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT id::text FROM tenants ORDER BY id")
            return [str(row[0]) for row in cur.fetchall()]


class Scheduler:
    """The tick loop: wake, ask every (job, tenant) pair whether it is due, run the due ones.

    Started from the FastAPI ``lifespan`` when ``settings.scheduler_enabled`` is on, exactly as
    :class:`~gateway.ingest_buffer.AsyncIngestBuffer` is. :meth:`tick_once` is public and performs
    one full pass, which is what the tests drive; :meth:`start` / :meth:`stop` wrap it in the
    background task.
    """

    def __init__(
        self,
        registry: JobRegistry,
        run_store: RunStore,
        tenant_provider: TenantProvider,
        *,
        tick_interval_s: float = 300.0,
        backoff_base_s: float = _DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = _DEFAULT_BACKOFF_CAP_S,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if tick_interval_s < _MIN_TICK_INTERVAL_S:
            raise ValueError(f"tick_interval_s must be >= {_MIN_TICK_INTERVAL_S}")
        self._registry = registry
        self._store = run_store
        self._tenants = tenant_provider
        self._tick_interval_s = float(tick_interval_s)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_cap_s = float(backoff_cap_s)
        self._now = now if now is not None else _utcnow
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ticks = 0

    @property
    def ticks(self) -> int:
        """Completed passes. Useful for tests and for an "is it alive" log line."""
        return self._ticks

    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def job_count(self) -> int:
        """How many jobs are registered. Zero is the correct answer for all of CTO-213."""
        return len(self._registry)

    async def start(self) -> None:
        """Launch the background tick loop (idempotent, like the ingest buffer's)."""
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="scheduler-tick")

    async def stop(self) -> None:
        """Signal the loop and wait for the current tick to finish. Never cancels mid-job.

        Waiting matters: a job is a blocking call on a worker thread that may be halfway through
        writing spans, and killing the loop out from under it would leave a run with no recorded
        outcome, which the next tick would read as "never ran". The tick checks the stop event
        between tenants, so a graceful shutdown costs at most one job's runtime.
        """
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = await self.tick_once()
                if result.ran:
                    logger.info(
                        "scheduler tick: ran=%d ok=%d skipped=%d failed=%d (considered=%d)",
                        result.ran,
                        result.succeeded,
                        result.skipped_by_job,
                        result.failed,
                        result.considered,
                    )
            except Exception:  # noqa: BLE001 - the loop outlives any single tick's failure
                # Reaching here means the tenant provider itself failed (per-job and per-tenant
                # failures are handled inside the tick). Postgres being briefly unreachable must
                # not end scheduling for the life of the process.
                logger.exception("scheduler tick failed; continuing")
            # Sleep the remainder of the interval, so the cadence does not drift by the tick's own
            # runtime, and wake immediately on stop rather than making shutdown wait out the sleep.
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.0, self._tick_interval_s - elapsed)
                )
            except asyncio.TimeoutError:
                pass

    async def tick_once(self) -> TickResult:
        """One full pass: for each job, for each tenant, run it if due. Returns what happened.

        Jobs and tenants are walked sequentially. These are hourly-or-daily jobs against
        rate-limited third-party billing APIs, so there is nothing to gain from fanning out, and a
        sequential walk keeps the load this puts on Postgres and on those APIs obvious.
        """
        tenants = await asyncio.to_thread(self._tenants)
        considered = ran = ok = skipped = failed = not_due = 0
        for job in self._registry.jobs():
            for tenant_id in tenants:
                if self._stopping:
                    break
                considered += 1
                try:
                    state = await asyncio.to_thread(self._store.get_state, job.name, tenant_id)
                except Exception:  # noqa: BLE001 - one unreadable row must not end the tick
                    logger.exception(
                        "scheduler: reading state for job=%s tenant=%s failed", job.name, tenant_id
                    )
                    continue
                if not is_due(
                    job,
                    state,
                    self._now(),
                    backoff_base_s=self._backoff_base_s,
                    backoff_cap_s=self._backoff_cap_s,
                ):
                    not_due += 1
                    continue
                ran += 1
                status = await self._invoke(job, tenant_id)
                if status == "success":
                    ok += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
        self._ticks += 1
        return TickResult(
            considered=considered,
            ran=ran,
            succeeded=ok,
            skipped_by_job=skipped,
            failed=failed,
            not_due=not_due,
        )

    @property
    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    async def _invoke(self, job: Job, tenant_id: str) -> RunStatus:
        """Run one job for one tenant off the event loop and record the outcome. Never raises.

        This wrapper is the failure isolation: whatever the job does, the caller gets a status back
        and the loop continues to the next tenant. The job body runs via :func:`asyncio.to_thread`
        because the things it will call (the cost connectors, the ingest workers, the reconciler)
        do blocking Postgres and HTTP work, and this loop shares a process with the API.
        """
        started_at = self._now()
        status: RunStatus = "success"
        error: str | None = None
        try:
            await asyncio.to_thread(job.fn, tenant_id)
        except JobSkipped as exc:
            status = "skipped"
            logger.debug("scheduler: job=%s tenant=%s skipped (%s)", job.name, tenant_id, exc)
        except Exception as exc:  # noqa: BLE001 - a job's failure is data, not a crash
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            # Not .exception(): a job that is broken for a tenant fails on every attempt, and a
            # stack trace per tick per tenant drowns the log. The scrubbed reason is on the row.
            logger.warning("scheduler: job=%s tenant=%s failed: %s", job.name, tenant_id, error)
        finished_at = self._now()
        try:
            await asyncio.to_thread(
                self._store.record_run,
                job.name,
                tenant_id,
                status,
                started_at=started_at,
                finished_at=finished_at,
                error_message=error,
            )
        except Exception:  # noqa: BLE001 - an unrecordable run must not stop the loop
            # The run happened but the history does not know it, so the next tick will run it again.
            # For an idempotent daily job that is a wasted repeat, which is the right way to fail.
            logger.exception("scheduler: recording job=%s tenant=%s failed", job.name, tenant_id)
        return status


def build_scheduler(
    settings: Settings,
    registry: JobRegistry | None = None,
    *,
    run_store: RunStore | None = None,
    tenant_provider: TenantProvider | None = None,
) -> Scheduler:
    """Construct the production scheduler from settings. Called from the gateway ``lifespan``.

    Registers NO jobs (CTO-213 is the engine; CTO-215 and CTO-216 add the first bodies), so with
    the default registry an enabled scheduler ticks over an empty set and does nothing.
    """
    return Scheduler(
        registry if registry is not None else JobRegistry(),
        run_store if run_store is not None else SchedulerRunStore(settings),
        tenant_provider if tenant_provider is not None else PostgresTenantProvider(settings),
        tick_interval_s=settings.scheduler_tick_interval_s,
        backoff_base_s=settings.scheduler_backoff_base_s,
        backoff_cap_s=settings.scheduler_backoff_cap_s,
    )


__all__ = [
    "Job",
    "JobFn",
    "JobRegistry",
    "JobSkipped",
    "JobState",
    "PostgresTenantProvider",
    "RunStatus",
    "RunStore",
    "Scheduler",
    "SchedulerRunStore",
    "TenantProvider",
    "TickResult",
    "backoff_delay_s",
    "build_scheduler",
    "is_due",
]
