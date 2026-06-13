# SPDX-License-Identifier: Apache-2.0
import random
import threading
import time

from tally.egress import (
    BackoffPolicy,
    BatchProcessor,
    FlakyTransport,
    MemoryTransport,
    ServerHints,
    TransportError,
)


def test_enqueue_and_flush_delivers():
    t = MemoryTransport()
    bp = BatchProcessor(t, max_batch_size=10)
    for i in range(5):
        bp.enqueue({"i": i})
    assert bp.flush_once() is True
    assert len(t.delivered) == 5
    assert bp.pending() == 0


def test_flush_empty_is_false():
    bp = BatchProcessor(MemoryTransport())
    assert bp.flush_once() is False


def test_batch_size_limit():
    t = MemoryTransport()
    bp = BatchProcessor(t, max_batch_size=3)
    for i in range(7):
        bp.enqueue({"i": i})
    assert bp.flush_once() is True
    assert len(t.batches[0]) == 3
    assert bp.pending() == 4


def test_drop_oldest_on_overflow_counts():
    bp = BatchProcessor(MemoryTransport(), max_buffer=3)
    for i in range(5):
        bp.enqueue({"i": i})
    assert bp.pending() == 3
    assert bp.obs.dropped_span_count == 2
    # oldest dropped → buffer holds 2,3,4
    bp.flush_once()
    delivered = bp.transport.delivered
    assert [s["i"] for s in delivered] == [2, 3, 4]


def test_failure_requeues_and_counts():
    t = FlakyTransport(fail_times=1)
    bp = BatchProcessor(t)
    bp.enqueue({"i": 1})
    assert bp.flush_once() is False  # first send fails
    assert bp.pending() == 1  # requeued
    assert bp.obs.internal_error_count == 1
    assert bp.flush_once() is True  # retry succeeds
    assert [s["i"] for s in t.delivered] == [1]


def test_backoff_grows_then_resets():
    rng = random.Random(0)
    bp = BatchProcessor(
        FlakyTransport(fail_times=3),
        backoff=BackoffPolicy(base_ms=100, jitter=0.0, rng=rng),
    )
    bp.enqueue({"i": 1})
    assert bp.flush_once() is False
    d1 = bp.current_backoff_ms()
    assert bp.flush_once() is False
    d2 = bp.current_backoff_ms()
    assert d2 > d1  # exponential growth
    bp.flush_once()  # 3rd failure
    bp.flush_once()  # success
    assert bp.current_backoff_ms() == 0.0  # reset after success


def test_backoff_capped():
    pol = BackoffPolicy(base_ms=100, max_ms=500, factor=2.0, jitter=0.0)
    assert pol.delay_ms(100) == 500  # capped


def test_honors_server_hints():
    class HintingTransport:
        def send(self, batch):
            return ServerHints(suggested_max_batch_size=2, suggested_flush_interval_ms=5000)

    bp = BatchProcessor(HintingTransport(), max_batch_size=512, flush_interval_ms=1000)
    bp.enqueue({"i": 1})
    bp.flush_once()
    assert bp.max_batch_size == 2
    assert bp.flush_interval_ms == 5000
    assert bp.last_hints is not None


def test_enqueue_never_raises_on_bad_state():
    bp = BatchProcessor(MemoryTransport())
    # enqueue is wrapped in the safety boundary; even a weird payload won't raise
    bp.enqueue({"ok": object()})
    assert bp.pending() == 1


def test_background_loop_drains(monkeypatch):
    t = MemoryTransport()
    bp = BatchProcessor(t, max_batch_size=100, flush_interval_ms=5)
    for i in range(50):
        bp.enqueue({"i": i})
    bp.start()
    # wait briefly for the daemon to flush
    deadline = time.time() + 2.0
    while time.time() < deadline and len(t.delivered) < 50:
        time.sleep(0.01)
    bp.stop()
    assert len(t.delivered) == 50


def test_transport_error_is_swallowed():
    class Boom:
        def send(self, batch):
            raise TransportError("down")

    bp = BatchProcessor(Boom())
    bp.enqueue({"i": 1})
    # must not raise
    assert bp.flush_once() is False
    assert threading.active_count() >= 1  # sanity: no crash
