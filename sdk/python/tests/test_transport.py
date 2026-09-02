# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §5 - background batching transport: batching, retry, backpressure, drain, envelope."""

from __future__ import annotations

import threading

from tally.egress import BackoffPolicy
from tally.transport import BatchingTransport
from tally.wire import decode_request


class _RecordingSender:
    """Captures POSTs; ``fail_times`` initial calls raise, then succeeds with 200."""

    def __init__(self, fail_times: int = 0, status: int = 200) -> None:
        self._fail = fail_times
        self._status = status
        self.calls: list[tuple[str, dict, bytes]] = []

    def __call__(self, url: str, headers: dict, body: bytes) -> int:
        self.calls.append((url, headers, body))
        if self._fail > 0:
            self._fail -= 1
            raise ConnectionError("simulated outage")
        return self._status


def _transport(sender, **kw) -> BatchingTransport:
    return BatchingTransport(
        "http://gw.test",
        "tally_sk_live_x",
        sdk_version="0.0.1",
        sender=sender,
        **kw,
    )


def test_export_never_blocks_and_flush_delivers():
    sender = _RecordingSender()
    t = _transport(sender)
    t.export({"gen_ai.system": "openai"})
    t.export({"gen_ai.system": "anthropic"})
    assert t.pending() == 2
    assert t.flush_once() is True
    assert t.pending() == 0
    assert len(sender.calls) == 1  # one batch


def test_envelope_has_bearer_and_empty_tenant():
    sender = _RecordingSender()
    t = _transport(sender)
    t.export({"gen_ai.system": "openai"})
    t.flush_once()
    url, headers, body = sender.calls[0]
    assert url == "http://gw.test/v1/batches"
    assert headers["Authorization"] == "Bearer tally_sk_live_x"
    req = decode_request(body.decode("utf-8"))
    # tenant omitted - the key decides at the gateway (CTO-260 §3.1).
    assert req.tenant_id == ""
    assert len(req.resource_spans) == 1


def test_retry_reuses_batch_id_and_eventually_delivers():
    sender = _RecordingSender(fail_times=2)
    t = _transport(sender)
    t.export({"gen_ai.system": "openai"})
    assert t.flush_once() is False  # attempt 1 fails
    assert t.flush_once() is False  # attempt 2 fails
    assert t.flush_once() is True  # attempt 3 succeeds
    # Same batch_id across retries (idempotent resend).
    ids = {decode_request(b.decode()).batch_id for (_, _, b) in sender.calls}
    assert len(ids) == 1


def test_retry_exhaustion_drops_batch_with_counter():
    sender = _RecordingSender(fail_times=99)
    t = _transport(sender, retry_max=3)
    t.export({"gen_ai.system": "openai"})
    for _ in range(3):
        t.flush_once()
    assert t.pending() == 0  # dropped after retry_max
    assert t.obs.dropped_span_count == 1


def test_backpressure_drops_oldest():
    sender = _RecordingSender()
    t = _transport(sender, max_buffer=2)
    t.export({"n": 1})
    t.export({"n": 2})
    t.export({"n": 3})  # overflow -> drop oldest ({"n": 1})
    assert t.pending() == 2
    assert t.obs.dropped_span_count == 1
    t.flush_once()
    delivered = decode_request(sender.calls[0][2].decode()).resource_spans
    assert {s["n"] for s in delivered} == {2, 3}


def test_flush_drains_all_batches():
    sender = _RecordingSender()
    t = _transport(sender, max_batch_size=1)
    for i in range(5):
        t.export({"n": i})
    t.flush(timeout=2.0)
    assert t.pending() == 0
    assert len(sender.calls) == 5


class _ConcurrentSender:
    """Thread-safe sender that fails every Nth call and records the spans each 2xx batch carried.

    Delivered batch ids and their span markers are captured under a lock so the test can assert,
    across a daemon flush racing a caller flush(), that every span is delivered exactly once.
    """

    def __init__(self, fail_every: int = 4) -> None:
        self._lock = threading.Lock()
        self._n = 0
        self.fail_every = fail_every
        self.delivered: list[int] = []
        self.delivered_batch_ids: list[str] = []

    def __call__(self, url: str, headers: dict, body: bytes) -> int:
        req = decode_request(body.decode("utf-8"))
        with self._lock:
            self._n += 1
            fail = self._n % self.fail_every == 0
            if not fail:
                self.delivered_batch_ids.append(req.batch_id)
                self.delivered.extend(int(s["i"]) for s in req.resource_spans)
        if fail:
            raise ConnectionError("simulated blip")
        return 200


def test_concurrent_flush_and_failures_lose_no_spans():
    # The daemon worker and a caller flush() run flush_once concurrently while sends intermittently
    # fail. The guarded flush path must lose no span, double-send none, and never crash the worker.
    sender = _ConcurrentSender(fail_every=4)
    t = _transport(
        sender,
        max_batch_size=8,
        retry_max=1000,  # high so no batch is dropped; the point is loss-free delivery
        flush_interval_s=0.001,
        backoff=BackoffPolicy(base_ms=0, max_ms=0),
    )
    total = 400
    t.start()
    try:
        for i in range(total):
            t.export({"i": i})
            if i % 5 == 0:
                t.flush(timeout=1.0)  # caller flush races the daemon worker
    finally:
        t.flush(timeout=5.0)
        t.stop(timeout=5.0)

    # Every exported span delivered exactly once: no loss, no duplicate.
    assert sorted(sender.delivered) == list(range(total))
    # A delivered batch id is never delivered twice (idempotent, pinned batch not double-sent).
    assert len(sender.delivered_batch_ids) == len(set(sender.delivered_batch_ids))
    assert t.pending() == 0


def test_pending_never_raises_under_concurrent_flush():
    # pending() reading _pending used to TOCTOU-crash the daemon (it runs outside safe_block).
    # Hammer it from many threads while flush_once churns _pending; it must never raise.
    sender = _RecordingSender(fail_times=50)
    t = _transport(sender, max_batch_size=1, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    for i in range(50):
        t.export({"i": i})

    errors: list[BaseException] = []
    stop = threading.Event()

    def _poll() -> None:
        try:
            while not stop.is_set():
                t.pending()
        except BaseException as exc:  # noqa: BLE001 - the whole point is that none escapes
            errors.append(exc)

    pollers = [threading.Thread(target=_poll) for _ in range(8)]
    for p in pollers:
        p.start()
    for _ in range(200):
        t.flush_once()
    stop.set()
    for p in pollers:
        p.join(timeout=2.0)
    assert errors == []


def test_sender_error_never_raises():
    def boom(url, headers, body):
        raise RuntimeError("kaboom")

    t = _transport(boom, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"n": 1})
    # Must not raise; failure recorded to self-observability.
    assert t.flush_once() is False
    assert t.obs.internal_error_count >= 1
