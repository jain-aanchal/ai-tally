"""Async ingest burst-buffer service: capacity, at-least-once, burst absorption, fairness (CTO-37).

These exercise :class:`gateway.ingest_buffer.AsyncIngestBuffer` — the service that wires the pure
buffer core into the gateway. Async behavior is driven with ``asyncio.run`` so no pytest plugin is
needed. Rows here are minimal tuples whose indices 2/3 are the TraceId/SpanId the buffer routes on
(matching ``gateway.mapping.COLUMNS``); the remaining columns are irrelevant to buffering.
"""

from __future__ import annotations

import asyncio
import threading
import time

from gateway.ingest_buffer import AsyncIngestBuffer


class FakeStore:
    """Records inserted rows; can fail a fixed number of times or sleep to simulate a slow CH."""

    def __init__(self, *, fail_times: int = 0, delay_s: float = 0.0) -> None:
        self.rows: list[tuple] = []
        self.fail_times = fail_times
        self.delay_s = delay_s
        self.calls = 0
        self._lock = threading.Lock()

    def insert_spans(self, rows: list[tuple]) -> int:
        with self._lock:
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError("clickhouse down")
        if self.delay_s:
            time.sleep(self.delay_s)
        with self._lock:
            self.rows.extend(rows)
        return len(rows)


def _row(tenant: str, trace: str, span: str) -> tuple:
    # (TenantId, Timestamp, TraceId, SpanId, ...) — only indices 2/3 matter to the buffer.
    return (tenant, "ts", trace, span, "payload")


def test_produce_counts_and_depth() -> None:
    buf = AsyncIngestBuffer(FakeStore())
    res = buf.produce_rows("t-acme", [_row("t-acme", f"tr{i}", f"s{i}") for i in range(5)])
    assert res.accepted == 5
    assert res.rejected == 0
    assert res.depth == 5
    assert buf.depth == 5


def test_capacity_sheds_overflow() -> None:
    buf = AsyncIngestBuffer(FakeStore(), capacity=3)
    res = buf.produce_rows("t", [_row("t", f"tr{i}", f"s{i}") for i in range(5)])
    assert res.accepted == 3
    assert res.rejected == 2  # shed, not crashed
    assert buf.dropped == 2
    assert buf.depth == 3


def test_drain_once_writes_and_dedups() -> None:
    store = FakeStore()
    buf = AsyncIngestBuffer(store)
    buf.produce_rows("t", [_row("t", "trA", "s1"), _row("t", "trA", "s2")])
    assert buf.drain_once() == 2
    assert len(store.rows) == 2
    # Re-producing the same identities and draining must not double-write (dedup).
    buf.produce_rows("t", [_row("t", "trA", "s1"), _row("t", "trA", "s2")])
    assert buf.drain_once() == 0
    assert len(store.rows) == 2


def test_drain_once_reenqueues_on_store_failure() -> None:
    store = FakeStore(fail_times=1)
    buf = AsyncIngestBuffer(store)
    buf.produce_rows("t", [_row("t", "trA", "s1")])
    # First drain: store raises → rows re-enqueued (at-least-once), nothing written.
    try:
        buf.drain_once()
        raise AssertionError("expected store failure to propagate")
    except RuntimeError:
        pass
    assert buf.depth == 1
    assert store.rows == []
    # Retry: writes exactly once.
    assert buf.drain_once() == 1
    assert len(store.rows) == 1


def _drain_until_empty(buf: AsyncIngestBuffer, timeout_s: float = 5.0) -> None:
    async def runner() -> None:
        await buf.start()
        deadline = time.monotonic() + timeout_s
        while buf.depth > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await buf.stop()  # stop() also flushes anything still buffered

    asyncio.run(runner())


def test_burst_is_absorbed_and_fully_drained() -> None:
    # A burst far larger than one drain batch, against a deliberately slow store, must be fully
    # written without the producer ever blocking or losing a row — the core CTO-37 guarantee.
    store = FakeStore(delay_s=0.002)
    buf = AsyncIngestBuffer(store, drain_batch=50, poll_interval_s=0.01)
    total = 1000
    produced = buf.produce_rows("t-acme", [_row("t-acme", f"tr{i}", f"s{i}") for i in range(total)])
    assert produced.accepted == total  # producing never blocked on the slow store
    assert produced.rejected == 0

    _drain_until_empty(buf)

    assert buf.depth == 0
    assert len(store.rows) == total
    # Every row landed exactly once.
    span_ids = {r[3] for r in store.rows}
    assert len(span_ids) == total


def test_drain_recovers_after_transient_store_outage() -> None:
    # Store is down for the first few drain attempts, then recovers; the loop must keep the rows and
    # land them all once CH is back (at-least-once across a transient outage).
    store = FakeStore(fail_times=3)
    buf = AsyncIngestBuffer(store, drain_batch=10, poll_interval_s=0.01)
    buf.produce_rows("t", [_row("t", f"tr{i}", f"s{i}") for i in range(20)])

    _drain_until_empty(buf)

    assert buf.depth == 0
    assert len(store.rows) == 20


def test_one_tenant_flood_does_not_starve_another() -> None:
    # Fairness is a *cross-partition* property: the partition hash includes tenant_id, so distinct
    # tenants land on distinct partitions and a fair drain serves each partition's head once per
    # round. (It deliberately does NOT reorder within a partition — that would break per-trace
    # ordering — so a tenant sharing a partition behind another's earlier records correctly waits;
    # real fairness comes from spreading across many partitions.) Here we put a hog's flood on every
    # partition *except* the small tenant's, so the small tenant owns its partition and must be
    # served on the very first drain round despite the flood.
    from gateway.partition import trace_partition

    small_partition = trace_partition("small", "small-trace")
    hog_traces = [t for i in range(500) if trace_partition("hog", (t := f"h{i}")) != small_partition]

    store = FakeStore()
    buf = AsyncIngestBuffer(store, drain_batch=4)  # >= partitions, so one round touches every partition
    buf.produce_rows("hog", [_row("hog", t, f"s{i}") for i, t in enumerate(hog_traces)])
    buf.produce_rows("small", [_row("small", "small-trace", "s0")])

    buf.drain_once()  # a single fair round
    assert any(r[0] == "small" for r in store.rows)  # small served immediately, not behind the flood
    assert any(r[0] == "hog" for r in store.rows)
    assert buf.depth > 0  # the hog's flood is still draining


def test_stop_without_start_is_noop() -> None:
    async def runner() -> None:
        await AsyncIngestBuffer(FakeStore()).stop()  # must not raise

    asyncio.run(runner())
