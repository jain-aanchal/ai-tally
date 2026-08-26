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

Multi-replica safety (CTO-214, S2). Two replicas with the flag on would otherwise run every job
twice for every tenant, which for the cost connectors means double-counted spend. Each (job, tenant)
pair is therefore guarded by a Postgres advisory lock (see :func:`advisory_lock_key` for the hash
and the lock space), and a replica that cannot take the lock leaves that tenant to whoever holds it.
Advisory locks specifically, rather than a lock table: the lock belongs to the session, so a replica
that crashes mid-job releases it the moment its connection dies. A lock table would need a lease and
a reaper to get the same property, and a reaper is one more thing that can silently stop.

A lock-contention skip is NOT a ``skipped`` run and records NO row. See :class:`TickResult` for the
argument: ``skipped`` settles the cadence window, and settling a window that another replica is at
this second working on would quietly cost a run.
"""

from __future__ import annotations

import asyncio
import hashlib
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
    # Due, but another replica holds the (job, tenant) advisory lock, so this replica did not run it
    # (CTO-214). Counted here and NOWHERE ELSE: contention writes no ``scheduler_runs`` row, and is
    # deliberately not a fourth status.
    #
    # WHY no row. ``skipped`` means "the job ran and there was nothing to do", and it settles the
    # cadence window so an unconfigured tenant is not re-asked every tick. Contention is the
    # opposite situation: the work is happening right now, on another replica, and this replica
    # must re-ask on the next tick. Recording anything that settles the window would mean a lock
    # collision silently costs a run, and a daily job would quietly become an every-other-day job
    # with nothing in the history to show for it. A fourth status would need a migration to the
    # 0027 CHECK constraint and would put a row in front of every future freshness surface that
    # means "no work was attempted", which is exactly the sort of thing that gets miscounted. The
    # honest record of "we tried and someone else had it" is a counter and a log line, not history:
    # nothing was invoked, so there is no invocation to log.
    lock_contended: int = 0


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


# --- Advisory locking (CTO-214, S2) ------------------------------------------------------------

# Personalisation for the key hash, mixed into the digest so this feature's keys are its own. It is
# a blake2b ``person``, which is capped at 16 bytes. Changing it changes every key, which would let
# an old replica and a new one both run the same job during a rolling deploy, so it is a constant.
_LOCK_KEY_PERSON = b"tally-sched-v1"


def advisory_lock_key(job_name: str, tenant_id: str) -> int:
    """The bigint ``pg_try_advisory_lock`` key for one (job, tenant) pair. Deterministic, stable.

    THE HASH. ``blake2b``, personalised with ``tally-sched-v1``, over the string
    ``f"{len(job_name)}\\x00{job_name}\\x00{tenant_id}"`` encoded UTF-8, digest truncated to 8
    bytes and read big-endian as a SIGNED integer, because a Postgres ``bigint`` is signed and an
    unsigned reading would overflow it for half of all inputs.

    The length prefix is not decoration. Without it, job ``"a"`` with tenant ``"b\\x00c"`` and job
    ``"a\\x00b"`` with tenant ``"c"`` would hash identically, and two unrelated jobs would
    serialise against each other for no reason a reader could ever guess.

    THE LOCK SPACE. Postgres has TWO advisory-lock spaces and they do NOT interact: the single
    ``bigint`` form ``pg_try_advisory_lock(bigint)`` and the two ``int32`` form
    ``pg_try_advisory_lock(int, int)``. Key ``0`` in the first is a different lock from ``(0, 0)``
    in the second. This code uses the SINGLE-BIGINT space, exclusively and deliberately: all 64 bits
    go to the hash, where the two-int form would spend 32 of them on a namespace field that buys
    nothing here. Anything else in this deployment that wants an advisory lock should take it in the
    two-int32 space, or go through this function.

    Collisions are possible in principle (64 bits, birthday bound: with ten thousand live (job,
    tenant) pairs the chance is about 3e-12). The consequence is bounded and safe in the direction
    that matters: two unrelated pairs would take turns, so one of them waits for the next tick.
    Never a double run, which is the property this ticket exists to buy.
    """
    digest = hashlib.blake2b(
        f"{len(job_name)}\x00{job_name}\x00{tenant_id}".encode(),
        digest_size=8,
        person=_LOCK_KEY_PERSON,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class LockHandle(Protocol):
    """A held lock. :meth:`release` must be safe to call exactly once, and must never raise."""

    def release(self) -> None: ...


class LockProvider(Protocol):
    """Exclusion for one (job, tenant) pair across replicas.

    ``acquire`` returns a handle when this process now holds the pair, or ``None`` when somebody
    else does. It never blocks waiting for the lock: a replica that waited would just be running the
    job late and twice in a row, and the whole point of the tick loop is that the next tick asks
    again anyway.
    """

    def acquire(self, job_name: str, tenant_id: str) -> LockHandle | None: ...


class _NullLock:
    """The handle used when no provider is configured. Releasing it is a no-op."""

    __slots__ = ()

    def release(self) -> None:
        return None


_NO_LOCK = _NullLock()


class PostgresAdvisoryLock:
    """A held session-scoped advisory lock, owning the connection whose session holds it.

    The connection is the lock. That is the whole reason for advisory locks over a lock table: if
    this process is SIGKILLed, or the container is evicted, or the network drops, Postgres tears the
    session down and the lock goes with it. Nothing has to notice the death and clean up, so there
    is no lease to tune and no reaper to forget to run.
    """

    __slots__ = ("_conn", "_key")

    def __init__(self, conn: psycopg.Connection, key: int) -> None:
        self._conn = conn
        self._key = key

    @property
    def key(self) -> int:
        return self._key

    def release(self) -> None:
        """Unlock and close. Never raises: a caller in a ``finally`` has nothing useful to do here.

        The explicit unlock is politeness for a pooled future; closing the connection is what
        actually guarantees the release, which is why the close is in the ``finally``.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
        except Exception:  # noqa: BLE001 - closing below releases it regardless
            logger.debug("scheduler: advisory unlock failed for key=%d; closing session", self._key)
        finally:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - a dead connection is already an unlocked one
                pass


class PostgresAdvisoryLockProvider:
    """``pg_try_advisory_lock`` keyed on job plus tenant, one dedicated session per held lock.

    Dedicated because the lock is scoped to the SESSION: it has to outlive the statement that took
    it and stay held for the whole run, and every other Postgres user in this module opens a
    connection per call and closes it. Borrowing one of those would release the lock the moment the
    query that took it finished, which is the failure mode that looks like locking works right up
    until two replicas overlap.

    One extra connection per job invocation is affordable here precisely because these are hourly
    and daily jobs: a tick that runs nothing opens none at all.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def acquire(self, job_name: str, tenant_id: str) -> PostgresAdvisoryLock | None:
        """Try, once, without waiting. ``None`` means another session holds this pair."""
        key = advisory_lock_key(job_name, tenant_id)
        # autocommit: session-level advisory locks are not transactional, and leaving an idle
        # transaction open for the length of a job would pin the xmin horizon for no reason.
        conn = psycopg.connect(self._dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                row = cur.fetchone()
            acquired = bool(row and row[0])
        except Exception:
            conn.close()
            raise
        if not acquired:
            conn.close()
            return None
        return PostgresAdvisoryLock(conn, key)


class Scheduler:
    """The tick loop: wake, ask every (job, tenant) pair whether it is due, run the due ones.

    Started from the FastAPI ``lifespan`` when ``settings.scheduler_enabled`` is on, exactly as
    :class:`~gateway.ingest_buffer.AsyncIngestBuffer` is. :meth:`tick_once` is public and performs
    one full pass, which is what the tests drive; :meth:`start` / :meth:`stop` wrap it in the
    background task.

    ``lock_provider`` (CTO-214) is what makes more than one replica safe. It is optional so the
    engine stays independent of Postgres for tests, and :func:`build_scheduler` always supplies the
    real one. With a single replica the outcome is identical either way: there is nobody to contend
    with, so every acquire succeeds and every pass does exactly what S1 did.
    """

    def __init__(
        self,
        registry: JobRegistry,
        run_store: RunStore,
        tenant_provider: TenantProvider,
        *,
        lock_provider: LockProvider | None = None,
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
        self._locks = lock_provider
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
                if result.ran or result.lock_contended:
                    logger.info(
                        "scheduler tick: ran=%d ok=%d skipped=%d failed=%d "
                        "contended=%d (considered=%d)",
                        result.ran,
                        result.succeeded,
                        result.skipped_by_job,
                        result.failed,
                        result.lock_contended,
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
        considered = ran = ok = skipped = failed = not_due = contended = 0
        for job in self._registry.jobs():
            for tenant_id in tenants:
                if self._stopping:
                    break
                considered += 1
                state = await self._read_state(job, tenant_id)
                if state is None:
                    continue
                if not self._due(job, state):
                    not_due += 1
                    continue
                # Due as far as this replica can tell. Take the lock BEFORE running anything: the
                # cheap read above is what keeps a tick from opening a lock session per tenant per
                # tick, and this is what keeps two replicas from both running the job (CTO-214).
                lock = await self._acquire(job, tenant_id)
                if lock is None:
                    contended += 1
                    continue
                try:
                    # Ask again, now that the pair is ours. Between the read above and the acquire,
                    # the replica that held the lock may have finished the very run we are about to
                    # repeat, and its row is committed before it releases. Without this second read
                    # the lock would prevent overlap but not duplication, which for a cost connector
                    # is the same double-counted spend by a slower route.
                    fresh = await self._read_state(job, tenant_id)
                    if fresh is None:
                        continue
                    if not self._due(job, fresh):
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
                finally:
                    # After :meth:`_invoke`, which means after the row is written. Releasing first
                    # would open exactly the window the re-read above closes, on every single run.
                    await self._release(job, tenant_id, lock)
        self._ticks += 1
        return TickResult(
            considered=considered,
            ran=ran,
            succeeded=ok,
            skipped_by_job=skipped,
            failed=failed,
            not_due=not_due,
            lock_contended=contended,
        )

    async def _read_state(self, job: Job, tenant_id: str) -> JobState | None:
        """Run history for one pair, or ``None`` if unreadable. One bad row must not end a tick."""
        try:
            return await asyncio.to_thread(self._store.get_state, job.name, tenant_id)
        except Exception:  # noqa: BLE001 - one unreadable row must not end the tick
            logger.exception(
                "scheduler: reading state for job=%s tenant=%s failed", job.name, tenant_id
            )
            return None

    def _due(self, job: Job, state: JobState) -> bool:
        return is_due(
            job,
            state,
            self._now(),
            backoff_base_s=self._backoff_base_s,
            backoff_cap_s=self._backoff_cap_s,
        )

    async def _acquire(self, job: Job, tenant_id: str) -> LockHandle | None:
        """Claim this (job, tenant) pair for this replica, or ``None`` to leave it alone.

        Fails CLOSED. If the lock cannot be taken for any reason, including Postgres refusing the
        connection, this replica does not run the job: without a held lock there is no way to rule
        out another replica running it right now, and the cost of not running is one tick's delay
        while the cost of running anyway is the double-counted spend this ticket exists to prevent.
        """
        if self._locks is None:
            return _NO_LOCK
        try:
            return await asyncio.to_thread(self._locks.acquire, job.name, tenant_id)
        except Exception as exc:  # noqa: BLE001 - an unavailable lock is a skip, not a crash
            logger.warning(
                "scheduler: could not take lock for job=%s tenant=%s, leaving it: %s: %s",
                job.name,
                tenant_id,
                type(exc).__name__,
                exc,
            )
            return None

    async def _release(self, job: Job, tenant_id: str, lock: LockHandle) -> None:
        """Release on every path out of the run, success and failure alike.

        A release that fails is survivable rather than fatal, which is the advisory-lock property
        again: the session holding it dies with this process, so the worst case is that one (job,
        tenant) pair is unavailable to the other replicas until then, not forever.
        """
        try:
            await asyncio.to_thread(lock.release)
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception(
                "scheduler: releasing lock for job=%s tenant=%s failed", job.name, tenant_id
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
    lock_provider: LockProvider | None = None,
) -> Scheduler:
    """Construct the production scheduler from settings. Called from the gateway ``lifespan``.

    Registers NO jobs (CTO-213 is the engine; CTO-215 and CTO-216 add the first bodies), so with
    the default registry an enabled scheduler ticks over an empty set and does nothing.

    Advisory locking is always on in this path (CTO-214). It needs no flag of its own: it uses the
    same Postgres the run history already requires, and a deployment that wanted it off would be
    asking for the double-counted spend it prevents.
    """
    return Scheduler(
        registry if registry is not None else JobRegistry(),
        run_store if run_store is not None else SchedulerRunStore(settings),
        tenant_provider if tenant_provider is not None else PostgresTenantProvider(settings),
        lock_provider=(
            lock_provider if lock_provider is not None else PostgresAdvisoryLockProvider(settings)
        ),
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
    "LockHandle",
    "LockProvider",
    "PostgresAdvisoryLock",
    "PostgresAdvisoryLockProvider",
    "PostgresTenantProvider",
    "RunStatus",
    "RunStore",
    "Scheduler",
    "SchedulerRunStore",
    "TenantProvider",
    "TickResult",
    "advisory_lock_key",
    "backoff_delay_s",
    "build_scheduler",
    "is_due",
]
