# SPDX-License-Identifier: Apache-2.0
"""Advisory locking: two replicas, one run (CTO-214, S2).

Two parts, and the split is deliberate.

The first part is fakes: the key hash, and what the tick loop does with a provider that hands out
or withholds locks. Those pin the DECISIONS (contention writes no row, the lock is released on the
success path and the failure path alike, a lock that cannot be taken means the job is not run).

The second part talks to a REAL Postgres, because the claim this ticket is actually making is about
Postgres, not about our code's opinion of Postgres. A mocked lock proves nothing about whether
``pg_try_advisory_lock`` excludes a second session, and it proves nothing at all about the property
we are buying the advisory lock FOR: that a replica which dies mid-job releases its lock. So those
two run against a live database, and are skipped when there is not one (CI has no Postgres; a
laptop with ``docker compose up`` does). ``TALLY_TEST_POSTGRES_DSN`` overrides the DSN.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from gateway.scheduler import (
    JobRegistry,
    JobSkipped,
    JobState,
    PostgresAdvisoryLockProvider,
    Scheduler,
    SchedulerRunStore,
    advisory_lock_key,
)

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
DAY = 86400.0

DEFAULT_DSN = "postgresql://tally:tally@localhost:5432/tally"


# --- the key hash ------------------------------------------------------------------------------


def test_key_is_deterministic():
    """Same pair, same key, forever. A key that moved would let two replicas both run a job."""
    assert advisory_lock_key("cost-connectors", "t1") == advisory_lock_key("cost-connectors", "t1")


def test_key_fits_a_signed_bigint():
    """The single-bigint lock space is signed 64-bit; an unsigned reading would not fit."""
    for job in ("a", "cost-connectors", "x" * 127):
        for tenant in ("t", str(uuid.uuid4()), "local-dev"):
            key = advisory_lock_key(job, tenant)
            assert -(2**63) <= key < 2**63


def test_key_separates_jobs_and_tenants():
    """Different job, or different tenant, means a different lock. Otherwise it over-serialises."""
    assert advisory_lock_key("job-a", "t1") != advisory_lock_key("job-b", "t1")
    assert advisory_lock_key("job-a", "t1") != advisory_lock_key("job-a", "t2")


def test_key_is_not_confusable_across_the_delimiter():
    """The length prefix earns its keep: ("a", "b\\x00c") must not collide with ("a\\x00b", "c").

    A plain ``job + "\\x00" + tenant`` hash collides on exactly this pair, and the resulting bug
    would be two unrelated jobs mysteriously taking turns.
    """
    assert advisory_lock_key("a", "b\x00c") != advisory_lock_key("a\x00b", "c")


# --- what the tick loop does with locks (fakes) --------------------------------------------------


class FakeRunStore:
    """Minimal in-memory ``scheduler_runs``, same shape as the one in test_scheduler.py."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def get_state(self, job_name: str, tenant_id: str) -> JobState:
        with self._lock:
            mine = [r for r in self.rows if r["job"] == job_name and r["tenant"] == tenant_id]
        if not mine:
            return JobState()
        settled = [r["finished_at"] for r in mine if r["status"] in ("success", "skipped")]
        last_settled = max(settled) if settled else None
        return JobState(
            last_success_at=max(
                (r["finished_at"] for r in mine if r["status"] == "success"), default=None
            ),
            last_settled_at=last_settled,
            last_attempt_at=max(r["finished_at"] for r in mine),
            consecutive_failures=sum(
                1
                for r in mine
                if r["status"] == "failed"
                and (last_settled is None or r["finished_at"] > last_settled)
            ),
        )

    def record_run(
        self,
        job_name: str,
        tenant_id: str,
        status: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            self.rows.append(
                {
                    "job": job_name,
                    "tenant": tenant_id,
                    "status": status,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": error_message,
                }
            )


class FakeLock:
    def __init__(self, owner: "FakeLockProvider", pair: tuple[str, str]) -> None:
        self._owner = owner
        self._pair = pair

    def release(self) -> None:
        self._owner.released.append(self._pair)
        self._owner.held.discard(self._pair)


class FakeLockProvider:
    """Hands out locks unless told otherwise. Records every acquire and every release."""

    def __init__(self, *, grant: bool = True, error: Exception | None = None) -> None:
        self.grant = grant
        self.error = error
        self.acquired: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []
        self.held: set[tuple[str, str]] = set()

    def acquire(self, job_name: str, tenant_id: str) -> FakeLock | None:
        pair = (job_name, tenant_id)
        self.acquired.append(pair)
        if self.error is not None:
            raise self.error
        if not self.grant:
            return None
        self.held.add(pair)
        return FakeLock(self, pair)


def _scheduler(store, locks, fn, *, tenants=("t1",), name="daily-job"):
    registry = JobRegistry()
    registry.register(name, DAY, fn)
    return Scheduler(
        registry,
        store,
        lambda: list(tenants),
        lock_provider=locks,
        tick_interval_s=60.0,
        now=lambda: T0,
    )


def test_lock_held_by_someone_else_does_not_run_and_records_nothing():
    """THE judgement call of this ticket: contention is not a ``skipped`` run.

    ``skipped`` settles the cadence window. If a lock collision settled it, the tenant would be
    told "your daily job ran today" when what actually happened is that nobody on this replica ran
    it. Whether the other replica finished is not this replica's business, so it writes nothing and
    the next tick asks again from the same history.
    """
    store = FakeRunStore()
    calls: list[str] = []
    locks = FakeLockProvider(grant=False)
    sched = _scheduler(store, locks, calls.append)

    result = asyncio.run(sched.tick_once())

    assert calls == []
    assert store.rows == []  # nothing settled, nothing claimed, no history invented
    assert result.lock_contended == 1
    assert result.ran == 0
    assert result.skipped_by_job == 0  # NOT conflated with a job saying "nothing to do"
    assert result.not_due == 0  # NOT conflated with being inside the cadence window either


def test_contention_leaves_the_job_due_on_the_next_tick():
    """The point of writing no row: the very next tick still considers the pair due."""
    store = FakeRunStore()
    calls: list[str] = []
    locks = FakeLockProvider(grant=False)
    sched = _scheduler(store, locks, calls.append)

    asyncio.run(sched.tick_once())
    locks.grant = True  # the other replica finished and let go
    result = asyncio.run(sched.tick_once())

    assert calls == ["t1"]
    assert result.ran == 1 and result.succeeded == 1


def test_lock_released_after_success():
    store = FakeRunStore()
    locks = FakeLockProvider()
    sched = _scheduler(store, locks, lambda t: None)

    asyncio.run(sched.tick_once())

    assert locks.released == [("daily-job", "t1")]
    assert locks.held == set()


def test_lock_released_after_failure():
    """The failure path releases too, or one broken job would wedge that tenant on every replica."""

    def boom(_tenant: str) -> None:
        raise RuntimeError("upstream 500")

    store = FakeRunStore()
    locks = FakeLockProvider()
    sched = _scheduler(store, locks, boom)

    result = asyncio.run(sched.tick_once())

    assert result.failed == 1
    assert locks.released == [("daily-job", "t1")]
    assert locks.held == set()


def test_lock_released_after_a_job_skip():
    def nothing_to_do(_tenant: str) -> None:
        raise JobSkipped("tenant has no connector configured")

    store = FakeRunStore()
    locks = FakeLockProvider()
    sched = _scheduler(store, locks, nothing_to_do)

    result = asyncio.run(sched.tick_once())

    assert result.skipped_by_job == 1
    assert locks.released == [("daily-job", "t1")]


def test_lock_released_before_the_next_tenant_is_locked():
    """Locks are per (job, tenant) and are not held across tenants, so one slow tenant blocks one."""
    store = FakeRunStore()
    locks = FakeLockProvider()
    sched = _scheduler(store, locks, lambda t: None, tenants=("t1", "t2", "t3"))

    asyncio.run(sched.tick_once())

    assert locks.acquired == [("daily-job", "t1"), ("daily-job", "t2"), ("daily-job", "t3")]
    assert locks.released == locks.acquired
    assert locks.held == set()


def test_lock_provider_failure_fails_closed():
    """Postgres unreachable for the lock means the job does NOT run. Not running is the safe half.

    A run without a confirmed lock could be the second one for that tenant today, and a second run
    of a cost connector is double-counted spend. A run delayed by a tick is a run delayed by a tick.
    """
    store = FakeRunStore()
    calls: list[str] = []
    locks = FakeLockProvider(error=RuntimeError("connection refused"))
    sched = _scheduler(store, locks, calls.append)

    result = asyncio.run(sched.tick_once())

    assert calls == []
    assert store.rows == []
    assert result.lock_contended == 1 and result.ran == 0


def test_state_is_reread_under_the_lock():
    """The lock closes the overlap window; this re-read closes the hand-off window.

    Replica B reads history, sees "due", and waits on the lock A holds. A finishes, commits its
    row, releases. If B then ran without asking again it would run the job a second time, having
    obeyed the lock perfectly the whole way. So B asks again with the pair in hand.
    """
    store = FakeRunStore()
    calls: list[str] = []

    class LateWriter(FakeLockProvider):
        def acquire(self, job_name: str, tenant_id: str):
            # Stand in for the other replica finishing between our read and our acquire.
            store.record_run(
                job_name, tenant_id, "success", started_at=T0, finished_at=T0
            )
            return super().acquire(job_name, tenant_id)

    locks = LateWriter()
    sched = _scheduler(store, locks, calls.append)

    result = asyncio.run(sched.tick_once())

    assert calls == []  # the other replica's run counts; we do not repeat it
    assert result.ran == 0 and result.not_due == 1
    assert locks.released == [("daily-job", "t1")]  # and we still let go
    assert len(store.rows) == 1  # exactly one run recorded for the pair


def test_no_lock_provider_is_s1_behaviour():
    """The provider is optional, and without one the loop is exactly what CTO-213 shipped."""
    store = FakeRunStore()
    calls: list[str] = []
    registry = JobRegistry()
    registry.register("daily-job", DAY, calls.append)
    sched = Scheduler(registry, store, lambda: ["t1", "t2"], now=lambda: T0)

    result = asyncio.run(sched.tick_once())

    assert calls == ["t1", "t2"]
    assert result.ran == 2 and result.succeeded == 2 and result.lock_contended == 0


# --- against a real Postgres ---------------------------------------------------------------------


def _dsn_or_skip() -> str:
    """The live DSN, or skip. A missing database must never turn CI red for a local-only test."""
    dsn = os.environ.get("TALLY_TEST_POSTGRES_DSN", DEFAULT_DSN)
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('scheduler_runs')")
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - no database here is a skip, not a failure
        pytest.skip(f"no Postgres at {dsn}: {type(exc).__name__}: {exc}")
    if row is None or row[0] is None:
        pytest.skip("scheduler_runs is missing; apply db/postgres/0027_scheduler_runs.sql")
    return dsn


def _key_components(key: int) -> tuple[int, int]:
    """Split a signed bigint lock key the way ``pg_locks`` reports it: high 32 bits, low 32 bits.

    Done in Python rather than SQL because reassembling the halves in SQL overflows ``bigint`` for
    any key with the high bit set, which is half of them.
    """
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


class _LiveSettings:
    def __init__(self, dsn: str) -> None:
        self.postgres_dsn = dsn


@pytest.fixture
def live_dsn():
    return _dsn_or_skip()


@pytest.fixture
def live_job(live_dsn):
    """A job name nothing else uses, cleaned up afterwards, so runs cannot collide or accumulate."""
    import psycopg

    name = f"test-lock-{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(live_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM scheduler_runs WHERE job_name = %s", (name,))
        conn.commit()


def test_live_two_runners_exactly_one_executes(live_dsn, live_job):
    """THE acceptance test. Two schedulers, one Postgres, one tenant, one due job. One run.

    Both are real: real ``SchedulerRunStore``, real ``PostgresAdvisoryLockProvider``, real
    ``pg_try_advisory_lock``. The job body blocks on an event so the two ticks genuinely overlap,
    which is the situation two gateway replicas are in every few minutes.
    """
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    settings = _LiveSettings(live_dsn)
    invocations: list[str] = []
    inside = threading.Event()
    may_finish = threading.Event()
    lock = threading.Lock()

    def body(tenant_id: str) -> None:
        with lock:
            invocations.append(tenant_id)
        inside.set()
        # Hold the run open until the other runner has had its turn at the lock.
        may_finish.wait(timeout=10.0)

    def build() -> Scheduler:
        registry = JobRegistry()
        registry.register(live_job, DAY, body)
        return Scheduler(
            registry,
            SchedulerRunStore(settings),
            lambda: [tenant],
            lock_provider=PostgresAdvisoryLockProvider(settings),
            tick_interval_s=60.0,
        )

    async def drive():
        first = asyncio.create_task(build().tick_once())
        # Wait until a job body is genuinely running (and therefore genuinely holding the lock)
        # before the second runner ticks. This is the overlap, not a simulation of one.
        await asyncio.to_thread(inside.wait, 10.0)
        second = await build().tick_once()
        may_finish.set()
        return await first, second

    winner, loser = asyncio.run(drive())

    assert invocations == [tenant], "the job body ran more than once across two runners"
    assert winner.ran == 1 and winner.succeeded == 1
    assert loser.ran == 0, "the second runner executed a job another runner was holding"
    assert loser.lock_contended == 1
    assert loser.skipped_by_job == 0  # contention is not a skip

    import psycopg

    with psycopg.connect(live_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM scheduler_runs WHERE job_name = %s AND tenant_id = %s",
            (live_job, tenant),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["success"], "contention wrote a row, or the job ran twice"


def test_live_lock_dies_with_the_connection(live_dsn, live_job):
    """The reason this is an advisory lock and not a lock table.

    A replica is killed mid-job. Nothing runs a cleanup, nothing renews a lease, nothing reaps. The
    session ends and Postgres drops the lock, so the next replica can pick the tenant straight up.
    Here the kill is ``pg_terminate_backend`` from another session, which is as close to a SIGKILL
    as a test can get without one.
    """
    import psycopg

    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    settings = _LiveSettings(live_dsn)
    provider = PostgresAdvisoryLockProvider(settings)
    key = advisory_lock_key(live_job, tenant)

    held = provider.acquire(live_job, tenant)
    assert held is not None
    assert held.key == key
    # Contended while it is held, as expected.
    assert provider.acquire(live_job, tenant) is None

    classid, objid = _key_components(key)
    with psycopg.connect(live_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_terminate_backend(l.pid)
            FROM pg_locks l
            WHERE l.locktype = 'advisory'
              AND l.classid::bigint = %s
              AND l.objid::bigint = %s
              AND l.objsubid = 1
              AND l.pid <> pg_backend_pid()
            """,
            (classid, objid),
        )
        killed = cur.fetchall()
        conn.commit()
    assert killed, "expected to find and kill the session holding the lock"

    # No cleanup, no lease expiry, no reaper: the lock is simply gone with the session.
    deadline = time.monotonic() + 5.0
    recovered = None
    while time.monotonic() < deadline:
        recovered = provider.acquire(live_job, tenant)
        if recovered is not None:
            break
        time.sleep(0.05)
    assert recovered is not None, "a dead session's advisory lock was not released"
    recovered.release()

    # And the original handle can still be released without raising, which is what the tick loop's
    # ``finally`` depends on: a crashed connection must not turn into an exception on the way out.
    held.release()


def test_live_single_runner_is_unchanged(live_dsn, live_job):
    """One replica with locking on behaves exactly as S1 did: due job runs, run is recorded."""
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    settings = _LiveSettings(live_dsn)
    calls: list[str] = []
    registry = JobRegistry()
    registry.register(live_job, DAY, calls.append)
    sched = Scheduler(
        registry,
        SchedulerRunStore(settings),
        lambda: [tenant],
        lock_provider=PostgresAdvisoryLockProvider(settings),
        tick_interval_s=60.0,
    )

    first = asyncio.run(sched.tick_once())
    second = asyncio.run(sched.tick_once())  # inside the cadence window now

    assert calls == [tenant]
    assert first.ran == 1 and first.succeeded == 1 and first.lock_contended == 0
    assert second.ran == 0 and second.not_due == 1 and second.lock_contended == 0


def test_live_lock_is_taken_in_the_single_bigint_space(live_dsn, live_job):
    """Pin the lock space, because the two forms do NOT exclude each other.

    ``pg_locks`` reports a single-bigint lock with ``classid`` holding the high 32 bits and
    ``objid`` the low ones, and ``objsubid = 1``; the two-int32 form reports ``objsubid = 2``.
    Anything that changed this code to the two-int form would silently stop excluding replicas
    still running the other spelling, and this assertion is what would catch it.
    """
    import psycopg

    tenant = f"tenant-{uuid.uuid4().hex[:8]}"
    provider = PostgresAdvisoryLockProvider(_LiveSettings(live_dsn))
    key = advisory_lock_key(live_job, tenant)
    handle = provider.acquire(live_job, tenant)
    assert handle is not None
    try:
        with psycopg.connect(live_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT objsubid
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid::bigint = %s
                  AND objid::bigint = %s
                """,
                _key_components(key),
            )
            rows = cur.fetchall()
        assert rows, "the advisory lock is not visible under its documented bigint key"
        assert all(r[0] == 1 for r in rows), "expected the single-bigint space (objsubid = 1)"
    finally:
        handle.release()
