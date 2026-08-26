# SPDX-License-Identifier: Apache-2.0
"""Scheduler core: due-calculation, bounded catch-up, failure isolation, shutdown (CTO-213).

No Postgres and no event-loop plugin: the run history is an in-memory fake implementing the
:class:`gateway.scheduler.RunStore` protocol, ``now`` is injected, and async behaviour is driven
with ``asyncio.run``, matching ``test_ingest_buffer.py``.

The point of these tests is the design claim in the module docstring: the scheduler keeps NO state
across a tick, so every decision is a pure function of the recorded history and the clock. That is
what makes "restart-safe" testable without restarting anything.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from gateway.scheduler import (
    Job,
    JobRegistry,
    JobSkipped,
    JobState,
    Scheduler,
    backoff_delay_s,
    is_due,
)

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
DAY = 86400.0


class FakeRunStore:
    """In-memory ``scheduler_runs``. Same queries as the real store, over a list."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.record_error: Exception | None = None
        self.state_error: Exception | None = None
        self._lock = threading.Lock()

    def get_state(self, job_name: str, tenant_id: str) -> JobState:
        if self.state_error is not None:
            raise self.state_error
        with self._lock:
            mine = [r for r in self.rows if r["job"] == job_name and r["tenant"] == tenant_id]
        if not mine:
            return JobState()
        successes = [r["finished_at"] for r in mine if r["status"] == "success"]
        settled = [r["finished_at"] for r in mine if r["status"] in ("success", "skipped")]
        last_settled = max(settled) if settled else None
        failures_since = [
            r
            for r in mine
            if r["status"] == "failed"
            and (last_settled is None or r["finished_at"] > last_settled)
        ]
        return JobState(
            last_success_at=max(successes) if successes else None,
            last_settled_at=last_settled,
            last_attempt_at=max(r["finished_at"] for r in mine),
            consecutive_failures=len(failures_since),
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
        if self.record_error is not None:
            raise self.record_error
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

    def statuses(self, job_name: str, tenant_id: str) -> list[str]:
        return [r["status"] for r in self.rows if r["job"] == job_name and r["tenant"] == tenant_id]


class MovableClock:
    """An injectable ``now`` a test can advance by hand. Nothing here ever sleeps for real."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _daily(fn) -> Job:
    return Job(name="daily-job", interval_s=DAY, fn=fn)


# --- due-calculation (the pure core) -----------------------------------------------------------


def test_due_when_never_run():
    """A job with no history at all is due immediately: a new job, or a new tenant."""
    assert is_due(_daily(lambda t: None), JobState(), T0) is True


def test_not_due_inside_the_window():
    """Ran an hour ago on a daily cadence: not due. This is the common case on every tick."""
    state = JobState(
        last_success_at=T0 - timedelta(hours=1),
        last_settled_at=T0 - timedelta(hours=1),
        last_attempt_at=T0 - timedelta(hours=1),
    )
    assert is_due(_daily(lambda t: None), state, T0) is False


def test_due_again_after_the_window():
    """Exactly one interval later it is due again. The boundary is inclusive: >= not >."""
    job = _daily(lambda t: None)
    last = T0 - timedelta(seconds=DAY)
    state = JobState(last_success_at=last, last_settled_at=last, last_attempt_at=last)
    assert is_due(job, state, T0) is True
    # One second short of the window is still inside it.
    assert is_due(job, state, T0 - timedelta(seconds=1)) is False


def test_a_skip_settles_the_window_but_is_not_a_success():
    """An unconfigured tenant is not re-asked every tick, and never looks like fresh data."""
    last = T0 - timedelta(hours=2)
    state = JobState(last_success_at=None, last_settled_at=last, last_attempt_at=last)
    assert is_due(_daily(lambda t: None), state, T0) is False
    assert state.last_success_at is None


def test_backoff_holds_a_repeatedly_failing_job_off():
    """Second failure onwards earns a growing gap; the first failure retries at the next tick."""
    assert backoff_delay_s(0) == 0.0
    assert backoff_delay_s(1) == 0.0  # a single blip is retried normally
    assert backoff_delay_s(2) == 300.0
    assert backoff_delay_s(3) == 600.0
    assert backoff_delay_s(20) == 6 * 3600.0  # capped, so it never stops retrying entirely

    job = _daily(lambda t: None)
    failed_at = T0 - timedelta(seconds=60)
    state = JobState(last_attempt_at=failed_at, consecutive_failures=3)
    # Never succeeded, so the cadence says "due", but backoff says wait.
    assert is_due(job, state, T0) is False
    assert is_due(job, state, T0 + timedelta(seconds=600)) is True


# --- bounded catch-up --------------------------------------------------------------------------


def test_bounded_catch_up_a_month_of_downtime_is_one_run():
    """The whole reason due-calculation is a boolean rather than a count of missed windows."""
    clock = MovableClock()
    store = FakeRunStore()
    calls: list[str] = []
    registry = JobRegistry()
    registry.register("daily-job", DAY, lambda tenant: calls.append(tenant))
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=60.0, now=clock)

    # Last ran 30 days ago. Thirty windows have elapsed.
    store.record_run(
        "daily-job",
        "t1",
        "success",
        started_at=clock.now - timedelta(days=30),
        finished_at=clock.now - timedelta(days=30),
    )
    result = asyncio.run(sched.tick_once())
    assert result.ran == 1 and result.succeeded == 1
    assert calls == ["t1"]

    # And the very next tick is quiet, because that one run settled the window.
    clock.advance(60)
    assert asyncio.run(sched.tick_once()).ran == 0
    assert calls == ["t1"]


# --- failure isolation -------------------------------------------------------------------------


def test_one_tenants_failure_does_not_stop_the_others():
    """Tenant b blows up; a and c still run, and b's failure is recorded rather than raised."""
    store = FakeRunStore()
    seen: list[str] = []

    def flaky(tenant: str) -> None:
        seen.append(tenant)
        if tenant == "b":
            raise RuntimeError("cost explorer returned 500")

    registry = JobRegistry()
    registry.register("daily-job", DAY, flaky)
    sched = Scheduler(registry, store, lambda: ["a", "b", "c"], tick_interval_s=60.0)

    result = asyncio.run(sched.tick_once())
    assert seen == ["a", "b", "c"]
    assert (result.ran, result.succeeded, result.failed) == (3, 2, 1)
    assert store.statuses("daily-job", "b") == ["failed"]
    assert store.statuses("daily-job", "a") == ["success"]
    # A failed run records the failure and produces nothing. The reason is kept, unscrubbed here
    # only because the fake store is standing in for the SQL layer that does the scrubbing.
    (row,) = [r for r in store.rows if r["tenant"] == "b"]
    assert "cost explorer returned 500" in row["error"]


def test_one_jobs_failure_does_not_stop_the_loop():
    """A job that fails for every tenant must not prevent the next job from running at all."""
    store = FakeRunStore()
    ran_second: list[str] = []
    registry = JobRegistry()
    registry.register("broken", DAY, lambda tenant: (_ for _ in ()).throw(ValueError("nope")))
    registry.register("healthy", DAY, lambda tenant: ran_second.append(tenant))
    sched = Scheduler(registry, store, lambda: ["t1", "t2"], tick_interval_s=60.0)

    result = asyncio.run(sched.tick_once())
    assert result.failed == 2
    assert result.succeeded == 2
    assert ran_second == ["t1", "t2"]


def test_a_skipping_job_records_skipped_not_success():
    """JobSkipped is the honest "nothing to do here", distinct from both success and failure."""
    store = FakeRunStore()
    registry = JobRegistry()
    registry.register(
        "daily-job", DAY, lambda tenant: (_ for _ in ()).throw(JobSkipped("not configured"))
    )
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=60.0)

    result = asyncio.run(sched.tick_once())
    assert (result.skipped_by_job, result.succeeded, result.failed) == (1, 0, 0)
    assert store.statuses("daily-job", "t1") == ["skipped"]
    assert store.get_state("daily-job", "t1").last_success_at is None


def test_an_unrecordable_run_does_not_stop_the_tick():
    """History being unwritable costs a repeat next tick; it must not end scheduling."""
    store = FakeRunStore()
    store.record_error = RuntimeError("postgres gone")
    seen: list[str] = []
    registry = JobRegistry()
    registry.register("daily-job", DAY, lambda tenant: seen.append(tenant))
    sched = Scheduler(registry, store, lambda: ["a", "b"], tick_interval_s=60.0)

    result = asyncio.run(sched.tick_once())
    assert seen == ["a", "b"]
    assert result.ran == 2


def test_repeated_failure_then_recovery_clears_the_backoff():
    """Backoff comes out of the history, so one good run resets it with no counter to clear."""
    clock = MovableClock()
    store = FakeRunStore()
    outcome = {"fail": True}

    def job(tenant: str) -> None:
        if outcome["fail"]:
            raise RuntimeError("still broken")

    registry = JobRegistry()
    registry.register("daily-job", DAY, job)
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=60.0, now=clock)

    assert asyncio.run(sched.tick_once()).failed == 1  # first failure, no backoff yet
    clock.advance(60)
    assert asyncio.run(sched.tick_once()).failed == 1  # second failure arms 300s of backoff
    clock.advance(60)
    assert asyncio.run(sched.tick_once()).not_due == 1  # serving backoff, not attempted

    clock.advance(300)
    outcome["fail"] = False
    assert asyncio.run(sched.tick_once()).succeeded == 1
    state = store.get_state("daily-job", "t1")
    assert state.consecutive_failures == 0
    assert state.last_success_at == clock.now


# --- the loop ----------------------------------------------------------------------------------


def test_start_and_stop_cleanly():
    """The lifespan contract: start creates the task, stop drains it and leaves nothing running."""
    store = FakeRunStore()
    ticked = threading.Event()
    registry = JobRegistry()
    registry.register("daily-job", DAY, lambda tenant: ticked.set())
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=1.0)

    async def drive() -> None:
        await sched.start()
        assert sched.running
        for _ in range(200):  # up to ~2s, far more than the first tick needs
            if ticked.is_set():
                break
            await asyncio.sleep(0.01)
        await sched.stop()

    asyncio.run(drive())
    assert ticked.is_set()
    assert not sched.running
    assert sched.ticks >= 1
    assert store.statuses("daily-job", "t1")[0] == "success"


def test_stop_waits_for_an_in_flight_job_rather_than_abandoning_it():
    """Shutdown mid-job must still record an outcome, or the run vanishes from history."""
    store = FakeRunStore()
    started = threading.Event()
    registry = JobRegistry()

    def slow(tenant: str) -> None:
        started.set()
        time.sleep(0.3)

    registry.register("slow-job", DAY, slow)
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=1.0)

    async def drive() -> None:
        await sched.start()
        while not started.is_set():
            await asyncio.sleep(0.01)
        await sched.stop()  # signalled while the job is still on its worker thread

    asyncio.run(drive())
    assert store.statuses("slow-job", "t1") == ["success"]


def test_stop_is_prompt_between_tenants():
    """A stop signalled mid-tick skips the remaining tenants instead of finishing the whole pass."""
    store = FakeRunStore()
    seen: list[str] = []
    gate = threading.Event()

    def job(tenant: str) -> None:
        seen.append(tenant)
        if tenant == "a":
            gate.set()
            time.sleep(0.2)

    registry = JobRegistry()
    registry.register("daily-job", DAY, job)
    tenants = [f"{c}" for c in "abcdefgh"]
    sched = Scheduler(registry, store, lambda: tenants, tick_interval_s=1.0)

    async def drive() -> None:
        await sched.start()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        await sched.stop()

    asyncio.run(drive())
    assert seen[0] == "a"
    assert len(seen) < len(tenants)  # the pass was abandoned, not run to completion


def test_a_job_does_not_block_the_event_loop():
    """The jobs this will call do blocking Postgres and HTTP work in a shared-with-the-API process.

    Asserted by keeping a coroutine ticking on the loop while a job sleeps on its worker thread: if
    the job body ran on the loop thread, the counter would not advance.
    """
    store = FakeRunStore()
    registry = JobRegistry()
    registry.register("blocking-job", DAY, lambda tenant: time.sleep(0.25))
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=60.0)
    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    async def drive() -> None:
        beat = asyncio.create_task(heartbeat())
        await sched.tick_once()
        beat.cancel()

    asyncio.run(drive())
    assert beats >= 5


# --- registry ----------------------------------------------------------------------------------


def test_registry_rejects_duplicates_and_nonsense_cadences():
    registry = JobRegistry()
    registry.register("a", DAY, lambda t: None)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", DAY, lambda t: None)
    with pytest.raises(ValueError, match="interval_s"):
        registry.register("b", 0, lambda t: None)
    assert len(registry) == 1
    assert registry.get("a") is not None and registry.get("zzz") is None


def test_an_empty_registry_is_a_no_op_which_is_all_of_cto_213():
    """CTO-213 registers no jobs. An enabled scheduler must therefore do exactly nothing."""
    store = FakeRunStore()
    sched = Scheduler(JobRegistry(), store, lambda: ["t1", "t2"], tick_interval_s=60.0)
    result = asyncio.run(sched.tick_once())
    assert (result.considered, result.ran) == (0, 0)
    assert store.rows == []


# --- bounded shutdown (CTO-219) -------------------------------------------------------------------


def test_stop_still_waits_when_the_job_finishes_inside_the_bound():
    """The bound does not make shutdown impatient. A normal job is waited for and recorded."""
    store = FakeRunStore()
    started = threading.Event()
    registry = JobRegistry()

    def slow(tenant: str) -> None:
        started.set()
        time.sleep(0.3)

    registry.register("slow-job", DAY, slow)
    sched = Scheduler(
        registry, store, lambda: ["t1"], tick_interval_s=1.0, shutdown_timeout_s=10.0
    )

    async def drive() -> None:
        await sched.start()
        while not started.is_set():
            await asyncio.sleep(0.01)
        await sched.stop()

    asyncio.run(drive())
    assert store.statuses("slow-job", "t1") == ["success"]
    assert not sched.abandoned  # waited it out, nothing orphaned
    assert not sched.running


def test_stop_is_bounded_when_a_job_will_not_finish():
    """A SIGTERM must not wait forever on an uncancellable job. THE acceptance case for CTO-219.

    The job here is wedged on an event, standing in for a connector waiting on a slow third-party
    billing API. ``asyncio.wait_for`` around the ``to_thread`` could not kill that thread and would
    only report a lie, which is why there is still no per-job timeout. What is bounded is how long
    SHUTDOWN waits before proceeding without it: the deploy finishes, and the thread dies with the
    process.

    Note what is asserted about the abandoned run: at the moment stop returns there is no row for
    the pair. That is the honest cost, and it is safe in the direction that matters, because a pair
    with no row is a pair the next tick considers due again.
    """
    store = FakeRunStore()
    started = threading.Event()
    release = threading.Event()
    registry = JobRegistry()

    def wedged(tenant: str) -> None:
        started.set()
        release.wait(timeout=30.0)  # released by the test, never by the scheduler

    registry.register("wedged-job", DAY, wedged)
    sched = Scheduler(registry, store, lambda: ["t1"], tick_interval_s=1.0, shutdown_timeout_s=0.2)

    async def drive() -> tuple[float, list[str]]:
        await sched.start()
        while not started.is_set():
            await asyncio.sleep(0.01)
        began = time.monotonic()
        await sched.stop()
        elapsed = time.monotonic() - began
        rows_at_stop = store.statuses("wedged-job", "t1")
        # Only now, so the orphaned worker thread does not outlive the test process.
        release.set()
        return elapsed, rows_at_stop

    elapsed, rows_at_stop = asyncio.run(drive())

    assert elapsed < 5.0, "shutdown waited on a job that was never going to finish"
    assert sched.abandoned
    assert not sched.running
    assert rows_at_stop == []  # the abandoned run is not recorded, so the pair stays due


def test_stop_on_a_scheduler_that_never_started_is_a_no_op():
    store = FakeRunStore()
    sched = Scheduler(JobRegistry(), store, lambda: ["t1"], tick_interval_s=1.0)
    asyncio.run(sched.stop())
    assert not sched.running and not sched.abandoned
