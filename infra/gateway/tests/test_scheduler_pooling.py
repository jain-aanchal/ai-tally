# SPDX-License-Identifier: Apache-2.0
"""Connection pooling on the scheduler's read path, and the lock path staying out of it (CTO-219).

The finding: the scheduler opened one fresh Postgres session per (job, tenant) per tick for the
state read, another per recorded run, and one per tick for the tenant list. At five jobs by a few
thousand tenants the connection handshakes alone cost more than the tick interval.

These tests measure that directly rather than asserting on an implementation detail. Every
connection is opened through a counting factory, so "how many sessions did a tick cost" is a number
the test can read, and the load-bearing claim is that the number does NOT grow with the tenant
count.

The second half is the constraint that makes pooling safe to do at all: an advisory lock is scoped
to its SESSION, so a lock connection can never come from, or go back to, a pool. Those tests pin
that the lock provider still opens its own dedicated connection per held lock and that the pool
never sees one.

Most of this needs no Postgres: the connections are fakes that answer the three statements the
scheduler runs. The last test does, and is skipped without one, exactly as
``test_scheduler_locking.py`` does: it counts real ``psycopg.connect`` calls against a real server
for a real tick, because a fake connection cannot prove anything about connection cost. The two
live tests that matter most to this change are over there and are untouched by it: two runners still
run a job once, and a lock still dies with its connection.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from gateway import scheduler as scheduler_module
from gateway.scheduler import (
    JobRegistry,
    PostgresAdvisoryLockProvider,
    PostgresTenantProvider,
    Scheduler,
    SchedulerConnectionPool,
    SchedulerRunStore,
)

DAY = 86400.0
DSN = "postgresql://tally:tally@localhost:5432/tally"


class _Settings:
    """Only what these classes actually read off Settings."""

    postgres_dsn = DSN


# --- fake connections ----------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._last = ""

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self._last = sql
        self._conn.factory.statements.append(sql)

    def fetchone(self):
        if "pg_try_advisory_lock" in self._last:
            return (self._conn.factory.grant_locks,)
        if "pg_advisory_unlock" in self._last:
            return (True,)
        return (None, None, None, 0)  # never-run state: due immediately

    def fetchall(self):
        return [(t,) for t in self._conn.factory.tenants]


class FakeConn:
    def __init__(self, factory: "CountingConnect") -> None:
        self.factory = factory
        self.closed = False
        self.broken = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True
        self.factory.closed += 1


class CountingConnect:
    """Stands in for ``psycopg.connect`` and counts the sessions actually opened."""

    def __init__(self, tenants: tuple[str, ...] = ("t1",), *, grant_locks: bool = True) -> None:
        self.opened = 0
        self.closed = 0
        self.live: list[FakeConn] = []
        self.statements: list[str] = []
        self.tenants = tenants
        self.grant_locks = grant_locks
        self._lock = threading.Lock()

    def __call__(self, dsn: str, **kwargs: object) -> FakeConn:
        with self._lock:
            self.opened += 1
        conn = FakeConn(self)
        self.live.append(conn)
        return conn


def _tick(tenants: tuple[str, ...], *, jobs: int = 2, max_idle: int = 4) -> CountingConnect:
    """One full tick over ``jobs`` x ``tenants`` with everything pooled. Returns the counter."""
    connect = CountingConnect(tenants)
    pool = SchedulerConnectionPool(DSN, max_idle=max_idle, connect=connect)
    registry = JobRegistry()
    for i in range(jobs):
        registry.register(f"job-{i}", DAY, lambda _tenant: None)
    sched = Scheduler(
        registry,
        SchedulerRunStore(_Settings(), pool=pool),
        PostgresTenantProvider(_Settings(), pool=pool),
        tick_interval_s=60.0,
        db_pool=pool,
    )
    result = asyncio.run(sched.tick_once())
    assert result.ran == jobs * len(tenants), "the tick did not do the work being measured"
    return connect


# --- how many sessions a tick costs ---------------------------------------------------------------


def test_a_tick_does_not_open_a_connection_per_job_per_tenant():
    """The finding, measured: 100 pairs used to mean hundreds of connects. Now it is a handful."""
    tenants = tuple(f"t{i}" for i in range(50))
    connect = _tick(tenants, jobs=2)

    # 100 pairs: a state read, a re-read under the lock, and a recorded run each, plus the listing.
    assert len(connect.statements) >= 300, "expected the per-pair queries to have actually run"
    assert connect.opened <= 4, f"a tick opened {connect.opened} sessions for 100 pairs"


def test_connection_count_does_not_grow_with_the_tenant_count():
    """The property that matters. Ten tenants and four hundred cost the same in sessions.

    This is what makes the tick interval a real interval again: connection overhead no longer
    scales with the population, so the loop is not spending its whole cadence on handshakes.
    """
    small = _tick(tuple(f"t{i}" for i in range(10)))
    large = _tick(tuple(f"t{i}" for i in range(200)))

    assert large.opened == small.opened
    assert len(large.statements) > 20 * small.opened  # the work grew; the sessions did not


def test_a_pooled_session_is_reused_rather_than_reopened():
    connect = CountingConnect()
    pool = SchedulerConnectionPool(DSN, connect=connect)
    store = SchedulerRunStore(_Settings(), pool=pool)

    for _ in range(25):
        store.get_state("job-a", "t1")

    assert connect.opened == 1
    assert pool.idle == 1


# --- the pool's hygiene ---------------------------------------------------------------------------


def test_a_broken_session_is_discarded_not_handed_on():
    """A server-side disconnect must not become the next borrower's problem."""
    connect = CountingConnect()
    pool = SchedulerConnectionPool(DSN, connect=connect)

    with pool.connection() as conn:
        first = conn
    first.broken = True  # the server went away while it sat idle

    with pool.connection() as conn:
        assert conn is not first
    assert connect.opened == 2
    assert first.closed


def test_a_failed_statement_discards_its_session():
    connect = CountingConnect()
    pool = SchedulerConnectionPool(DSN, connect=connect)

    with pytest.raises(RuntimeError):
        with pool.connection() as conn:
            borrowed = conn
            raise RuntimeError("query blew up")

    assert borrowed.closed
    assert pool.idle == 0


def test_a_closed_pool_still_serves_a_borrow_but_keeps_nothing_warm():
    """Shutdown closes the pool while an abandoned job may still be recording its run (CTO-219).

    Refusing that borrow would turn "the run finished after we stopped waiting" into "the run
    finished and the history lost it", so a closed pool only stops REUSING sessions.
    """
    connect = CountingConnect()
    pool = SchedulerConnectionPool(DSN, connect=connect)
    with pool.connection():
        pass
    assert pool.idle == 1

    pool.close()
    assert pool.idle == 0
    pool.close()  # idempotent

    with pool.connection() as conn:
        late = conn
        assert not late.closed, "a closed pool refused a borrow an in-flight job still needed"
    assert late.closed, "a closed pool kept the session alive after the borrow"
    assert pool.idle == 0, "a closed pool kept a session warm"


def test_stopping_the_scheduler_closes_its_pool():
    connect = CountingConnect()
    pool = SchedulerConnectionPool(DSN, connect=connect)
    store = SchedulerRunStore(_Settings(), pool=pool)
    store.get_state("job-a", "t1")
    assert pool.idle == 1

    sched = Scheduler(
        JobRegistry(), store, lambda: ["t1"], tick_interval_s=60.0, db_pool=pool
    )
    asyncio.run(sched.stop())  # never started: still tidies up the sessions

    assert pool.idle == 0
    assert connect.closed >= 1


# --- the constraint: locks are never pooled --------------------------------------------------------


def test_the_lock_provider_opens_its_own_session_per_lock(monkeypatch):
    """NOT NEGOTIABLE. The lock is the session, so it can never come from a pool.

    A pooled connection returned while its advisory lock was still held would hand the next
    borrower a session silently owning a lock, and would release that lock at a moment nobody
    chose. It would also destroy the reason CTO-214 chose advisory locks: the lock dying with the
    process, no lease, no reaper. So the provider connects for itself, every time.
    """
    connect = CountingConnect()
    monkeypatch.setattr(scheduler_module.psycopg, "connect", connect)
    pool = SchedulerConnectionPool(DSN, connect=CountingConnect())
    provider = PostgresAdvisoryLockProvider(_Settings())

    handles = [provider.acquire("job-a", f"t{i}") for i in range(3)]

    assert all(h is not None for h in handles)
    assert connect.opened == 3, "held locks shared a session"
    assert pool.opened == 0, "a lock session came from the read pool"
    assert len({id(h._conn) for h in handles}) == 3  # noqa: SLF001 - the point of the assertion

    for handle in handles:
        handle.release()
    assert connect.closed == 3, "a lock session outlived its lock instead of being closed"


def test_a_released_lock_session_is_not_returned_to_the_pool(monkeypatch):
    """Release closes the session outright. Nothing about a lock ever reaches the pool's idle list."""
    connect = CountingConnect()
    monkeypatch.setattr(scheduler_module.psycopg, "connect", connect)
    pool = SchedulerConnectionPool(DSN, connect=connect)
    provider = PostgresAdvisoryLockProvider(_Settings())

    handle = provider.acquire("job-a", "t1")
    assert handle is not None
    handle.release()

    assert pool.idle == 0
    assert connect.live[0].closed


def test_a_contended_lock_closes_its_session_immediately(monkeypatch):
    """A refused lock must not leak the session it asked with, pooled or otherwise."""
    connect = CountingConnect(grant_locks=False)
    monkeypatch.setattr(scheduler_module.psycopg, "connect", connect)
    provider = PostgresAdvisoryLockProvider(_Settings())

    assert provider.acquire("job-a", "t1") is None
    assert connect.closed == 1


# --- against a real Postgres ----------------------------------------------------------------------


def _live_dsn_or_skip() -> str:
    """The live DSN, or skip. Same posture as ``test_scheduler_locking.py``: CI has no Postgres."""
    import os

    dsn = os.environ.get("TALLY_TEST_POSTGRES_DSN", DSN)
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


class _LiveSettings:
    def __init__(self, dsn: str) -> None:
        self.postgres_dsn = dsn


def test_live_a_tick_over_many_tenants_opens_a_handful_of_sessions():
    """The measurement that matters, against a real server: real connects, counted.

    Twenty-five tenants means twenty-five state reads, twenty-five re-reads under the lock and
    twenty-five recorded runs on the pooled path. Before CTO-219 that was seventy-five connects
    (plus one per lock). The pool's own counter is the instrument, and it counts real
    ``psycopg.connect`` calls to a real Postgres.

    The advisory-lock sessions are NOT in this number, and must not be: they are still one dedicated
    session per held lock, which is what makes a lock die with the process.
    """
    import uuid

    import psycopg

    dsn = _live_dsn_or_skip()
    job_name = f"test-pool-{uuid.uuid4().hex[:12]}"
    tenants = [f"tenant-{uuid.uuid4().hex[:8]}" for _ in range(25)]
    pool = SchedulerConnectionPool(dsn, max_idle=4)
    registry = JobRegistry()
    registry.register(job_name, DAY, lambda _tenant: None)
    sched = Scheduler(
        registry,
        SchedulerRunStore(_LiveSettings(dsn), pool=pool),
        lambda: list(tenants),
        lock_provider=PostgresAdvisoryLockProvider(_LiveSettings(dsn)),
        tick_interval_s=60.0,
        db_pool=pool,
    )
    try:
        result = asyncio.run(sched.tick_once())
        assert result.ran == 25 and result.succeeded == 25
        assert pool.opened <= 4, f"{pool.opened} sessions for 25 tenants; pooling is not working"

        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM scheduler_runs WHERE job_name = %s", (job_name,))
            recorded = cur.fetchone()[0]
        assert recorded == 25, "pooled autocommit sessions did not durably record every run"
    finally:
        pool.close()
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM scheduler_runs WHERE job_name = %s", (job_name,))
            conn.commit()
