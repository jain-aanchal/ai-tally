# SPDX-License-Identifier: Apache-2.0
"""Lifespan shutdown ordering: the ingest buffer is flushed first, whatever the scheduler is doing.

The finding (CTO-219, review item 4a): ``lifespan`` stopped the scheduler BEFORE flushing the
ingest buffer. A job that was mid-run when SIGTERM arrived held shutdown open, and the buffered
spans behind it died with the process. The buffer holds accepted customer telemetry that is not
durable anywhere yet; the scheduler holds work that the next tick re-derives from recorded history.
So the buffer goes first, and its flush must not be able to queue behind a job at all.

These drive the real ``lifespan`` through ``TestClient`` and substitute the two background objects
it stops, because what is under test is the ORDER app.py stops things in, not what those two do
(``test_ingest_buffer.py`` and ``test_scheduler.py`` cover that). The store is faked so no
ClickHouse is needed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from gateway import app as app_module
from gateway.app import app


class FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def insert_spans(self, rows: list[tuple]) -> int:
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


@contextmanager
def _client(store: FakeStore) -> Iterator[TestClient]:
    orig_factory = app_module.ClickHouseStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]


class SlowScheduler:
    """A scheduler with a job in flight: its stop takes a while, exactly as a real one may."""

    def __init__(self, order: list[str], *, delay_s: float = 0.2) -> None:
        self._order = order
        self._delay_s = delay_s

    async def stop(self) -> None:
        self._order.append("scheduler-stop-entered")
        await asyncio.sleep(self._delay_s)
        self._order.append("scheduler-stopped")


class WedgedScheduler:
    """A scheduler whose stop never returns. Stands in for one whose bound has been reached."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def stop(self) -> None:
        self._order.append("scheduler-stop-entered")
        await asyncio.sleep(3600)


class RecordingBuffer:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.flushed = False

    async def stop(self) -> None:
        self._order.append("buffer-flushed")
        self.flushed = True


def test_the_buffer_is_flushed_before_the_scheduler_is_waited_on():
    """Ordering, plainly: nothing about the flush is allowed to queue behind a running job."""
    order: list[str] = []
    store = FakeStore()
    buffer = RecordingBuffer(order)

    with _client(store):
        app.state.scheduler = SlowScheduler(order)
        app.state.ingest_buffer = buffer

    assert order[0] == "buffer-flushed", f"buffer flushed after the scheduler: {order}"
    assert order == ["buffer-flushed", "scheduler-stop-entered", "scheduler-stopped"]
    assert buffer.flushed
    assert store.closed  # and the store still closes last, after both


def test_the_buffer_is_flushed_even_when_the_scheduler_never_stops():
    """A stop that never returns must not cost the buffered spans. The wedged case, end to end.

    ``WedgedScheduler.stop`` sleeps for an hour, which is what the OLD ordering did to shutdown for
    real whenever a job was mid-run. The lifespan is driven directly here (rather than through
    ``TestClient``, which would wait out that hour) with a ceiling on the whole shutdown, and the
    assertion is that the flush is already done by the time anything waits on the scheduler.
    """
    order: list[str] = []
    store = FakeStore()
    buffer = RecordingBuffer(order)

    async def run_lifespan() -> None:
        orig_factory = app_module.ClickHouseStore
        app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
        try:
            ctx = app_module.lifespan(app)
            await ctx.__aenter__()
            app.state.scheduler = WedgedScheduler(order)
            app.state.ingest_buffer = buffer
            try:
                await asyncio.wait_for(ctx.__aexit__(None, None, None), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # the wedged scheduler, exactly as intended
        finally:
            app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
            app.state.scheduler = None
            app.state.ingest_buffer = None

    asyncio.run(run_lifespan())

    assert buffer.flushed, "the buffer was not flushed while the scheduler was wedged"
    assert order == ["buffer-flushed", "scheduler-stop-entered"]


def test_the_real_scheduler_does_not_hold_the_lifespan_open(monkeypatch):
    """4a and 4b together: buffer flushed, and shutdown bounded, with a real job mid-run.

    A job wedged on an event is the connector stuck on a slow billing API. The lifespan must exit
    anyway, and the telemetry must already be safe when it does.
    """
    from gateway.scheduler import JobRegistry, JobState, Scheduler

    class MemoryRunStore:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str, str]] = []

        def get_state(self, job_name: str, tenant_id: str) -> JobState:
            return JobState()  # never run, therefore due

        def record_run(self, job_name, tenant_id, status, **kwargs) -> None:
            self.rows.append((job_name, tenant_id, status))

    order: list[str] = []
    store = FakeStore()
    buffer = RecordingBuffer(order)
    running = threading.Event()
    release = threading.Event()

    def wedged(_tenant: str) -> None:
        running.set()
        # Bounded only so the orphaned thread cannot outlive the test; the scheduler never ends it.
        release.wait(timeout=10.0)

    registry = JobRegistry()
    registry.register("wedged-job", 86400.0, wedged)
    run_store = MemoryRunStore()
    sched = Scheduler(
        registry, run_store, lambda: ["t1"], tick_interval_s=1.0, shutdown_timeout_s=0.2
    )

    async def run_lifespan() -> float:
        orig_factory = app_module.ClickHouseStore
        app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
        try:
            ctx = app_module.lifespan(app)
            await ctx.__aenter__()
            app.state.scheduler = sched
            app.state.ingest_buffer = buffer
            await sched.start()
            while not running.is_set():
                await asyncio.sleep(0.01)
            began = time.monotonic()
            await ctx.__aexit__(None, None, None)
            elapsed = time.monotonic() - began
            release.set()  # let the orphan finish rather than leaving it to the test runner
            return elapsed
        finally:
            app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
            app.state.scheduler = None
            app.state.ingest_buffer = None

    elapsed = asyncio.run(run_lifespan())

    assert elapsed < 5.0, "a job mid-run held the lifespan open"
    assert buffer.flushed and order[0] == "buffer-flushed"
    assert sched.abandoned
    assert run_store.rows == []  # the abandoned run recorded nothing, so the pair stays due
    assert store.closed
