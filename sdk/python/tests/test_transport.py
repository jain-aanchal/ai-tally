# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §5 - background batching transport: batching, retry, backpressure, drain, envelope."""

from __future__ import annotations

import threading

from tally.egress import BackoffPolicy
from tally.transport import BatchingTransport, SendResult, _retry_hint_ms
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


# --- #315: bounded retry that honors the gateway's own instruction ---


class _ScriptedSender:
    """Replays a scripted list of answers, then 200s. Records every (headers, body) POSTed.

    An entry is a ``SendResult``, a bare status int (the older sender shape), or an exception
    instance to raise (a transport-level failure).
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, dict, bytes]] = []

    def __call__(self, url: str, headers: dict, body: bytes):
        self.calls.append((url, headers, body))
        if not self._script:
            return SendResult(200)
        nxt = self._script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def test_503_retry_hint_zero_succeeds_on_attempt_two():
    # The exact shape that killed a backfill at 450k of 512,056 spans: the gateway shed under load
    # and said "retry", with server_hints.retry_after_ms = 0.
    sender = _ScriptedSender([SendResult(503, 0)])
    t = _transport(sender)
    t.export({"gen_ai.system": "openai"})
    assert t.flush_once() is False  # shed answer, batch held, not thrown away
    assert t.pending() == 1
    assert t.flush_once() is True
    assert len(sender.calls) == 2
    assert t.shed_counts() == {
        "undelivered_span_count": 0,
        "rejected_span_count": 0,
        "buffer_overflow_span_count": 0,
    }


def test_resend_is_byte_identical():
    # The property that keeps a retry a replay rather than a duplicate: identical bytes means a
    # stable batch_id, which the gateway's (tenant_id, batch_id) idempotency store recognizes.
    # Duplicate spans permanently pollute the SummingMergeTree rollups (#311).
    sender = _ScriptedSender([SendResult(503, 0), ConnectionError("blip"), SendResult(500)])
    t = _transport(sender)
    t.export({"gen_ai.system": "openai", "n": 1})
    t.export({"gen_ai.system": "anthropic", "n": 2})
    for _ in range(4):
        t.flush_once()
    assert len(sender.calls) == 4
    bodies = {body for (_, _, body) in sender.calls}
    assert len(bodies) == 1  # byte-for-byte identical, not merely the same batch_id
    assert len({decode_request(b.decode()).batch_id for b in bodies}) == 1


def test_retry_after_hint_is_honored_and_clamped():
    sender = _ScriptedSender([SendResult(429, 3_000)])
    t = _transport(sender, backoff=BackoffPolicy(base_ms=10, max_ms=30_000))
    t.export({"n": 1})
    t.flush_once()
    # The gateway's wait wins over our own (which would be ~10ms here).
    assert t.current_backoff_ms() == 3_000
    # ...but is clamped to the ceiling, so a server asking for a five minute pause cannot stall us.
    t2 = _transport(
        _ScriptedSender([SendResult(429, 300_000)]), backoff=BackoffPolicy(max_ms=2_000)
    )
    t2.export({"n": 1})
    t2.flush_once()
    assert t2.current_backoff_ms() == 2_000


def test_retry_after_zero_falls_back_to_our_backoff():
    # "No specific delay" is not licence to hammer a gateway that is already shedding.
    sender = _ScriptedSender([SendResult(503, 0)])
    t = _transport(sender, backoff=BackoffPolicy(base_ms=100, max_ms=1_000, jitter=0.0))
    t.export({"n": 1})
    t.flush_once()
    assert t.current_backoff_ms() == 100


def test_retry_bound_exhausted_sheds_and_counts():
    sender = _ScriptedSender([SendResult(503, 0)] * 10)
    t = _transport(sender, retry_max=3)
    for i in range(4):
        t.export({"n": i})
    assert t.flush_once() is False
    assert t.flush_once() is False
    assert t.flush_once() is False
    assert len(sender.calls) == 3  # bounded: a down gateway fails fast, it does not hang forever
    assert t.pending() == 0
    # Shed, and COUNTED. The count is what makes the loss honest rather than silent.
    assert t.shed_counts()["undelivered_span_count"] == 4
    assert t.obs.dropped_span_count == 4


def test_non_retryable_status_is_not_retried():
    sender = _ScriptedSender([SendResult(400)])
    t = _transport(sender, retry_max=5)
    t.export({"n": 1})
    assert t.flush_once() is False
    assert t.pending() == 0  # terminal on the first answer, no resend
    assert len(sender.calls) == 1
    assert t.shed_counts()["rejected_span_count"] == 1
    assert t.shed_counts()["undelivered_span_count"] == 0


def test_unauthorized_is_not_retried():
    sender = _ScriptedSender([SendResult(401)])
    t = _transport(sender, retry_max=5)
    t.export({"n": 1})
    t.flush_once()
    t.flush_once()  # buffer empty now; must not have re-sent the refused batch
    assert len(sender.calls) == 1
    assert t.shed_counts()["rejected_span_count"] == 1


def test_bare_status_sender_still_works():
    # The older Sender shape (an int) stays supported for callers with their own sender.
    sender = _ScriptedSender([503, 200])
    t = _transport(sender)
    t.export({"n": 1})
    assert t.flush_once() is False
    assert t.flush_once() is True


def test_retry_hint_parsing():
    # Header form (delay-seconds) wins over the body.
    assert _retry_hint_ms({"Retry-After": "2"}, b'{"server_hints":{"retry_after_ms":9000}}') == 2000
    # The 503 shed body: a real 0, distinct from "no hint at all".
    assert _retry_hint_ms({}, b'{"status":"retry","server_hints":{"retry_after_ms":0}}') == 0
    # The 429 body puts it at the top level (gateway/app.py).
    assert _retry_hint_ms({}, b'{"retry_after_ms":1500}') == 1500
    # Garbage never stalls the worker: no hint, fall back to our own backoff.
    assert _retry_hint_ms({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, b"") is None
    assert _retry_hint_ms({}, b"not json") is None
    assert _retry_hint_ms({}, b'{"retry_after_ms":"soon"}') is None


# --- #315 review: the hot-path invariant, damped shed logging, directly tracked counters ---


def test_encode_happens_outside_the_lock(monkeypatch):
    # The lock is held only for brief state transitions, never for work: serializing a full batch
    # under it stalls every export() on the hot path once per flush, which the module docstring and
    # CTO-260 §5 forbid. Probe it from inside the encode: _lock is not reentrant, so a successful
    # non-blocking acquire on the flushing thread proves the encode is not holding it.
    import tally.transport as transport_mod

    real_encode = transport_mod.encode_request
    seen: list[bool] = []
    t = _transport(_RecordingSender())

    def probing_encode(batch):
        free = t._lock.acquire(blocking=False)
        seen.append(free)
        if free:
            t._lock.release()
        return real_encode(batch)

    monkeypatch.setattr(transport_mod, "encode_request", probing_encode)
    for i in range(3):
        t.export({"n": i})
    assert t.flush_once() is True
    assert seen == [True]  # encoded exactly once, and with the lock free


def test_export_is_not_blocked_by_an_in_flight_encode(monkeypatch):
    # The same invariant from the producer's side: a slow encode must not hold up export().
    import tally.transport as transport_mod

    real_encode = transport_mod.encode_request
    in_encode = threading.Event()
    release_encode = threading.Event()
    t = _transport(_RecordingSender())

    def slow_encode(batch):
        in_encode.set()
        release_encode.wait(timeout=5.0)
        return real_encode(batch)

    monkeypatch.setattr(transport_mod, "encode_request", slow_encode)
    t.export({"n": 0})
    flusher = threading.Thread(target=t.flush_once)
    flusher.start()
    assert in_encode.wait(timeout=5.0)
    exported = threading.Thread(target=t.export, args=({"n": 1},))
    exported.start()
    exported.join(timeout=2.0)
    assert not exported.is_alive()  # would hang if the encode ran under _lock
    release_encode.set()
    flusher.join(timeout=5.0)
    assert t.pending() == 1  # the span exported mid-flush is still queued, not lost


def test_shed_warning_is_damped_per_cause(caplog):
    # A rotated key answers 401 on every flush forever. The counters stay exact; the log must not
    # emit one WARNING per flush for the life of the process.
    import logging

    from tally.transport import _SHED_LOG_EVERY

    t = _transport(_ScriptedSender([SendResult(401)] * 400))
    with caplog.at_level(logging.WARNING, logger="tally"):
        for i in range(_SHED_LOG_EVERY + 1):
            t.export({"n": i})
            t.flush_once()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # One line for the first shed, one rolled-up summary at the interval. Not 101 lines.
    assert len(warnings) == 2
    assert "shed 1 span(s)" in warnings[0].getMessage()
    assert "status 401" in warnings[1].getMessage()
    # The record is the counters, and it is complete.
    assert t.shed_counts()["rejected_span_count"] == _SHED_LOG_EVERY + 1


def test_buffer_overflow_count_is_not_polluted_by_another_component(caplog):
    # obs is shareable, and BatchProcessor increments dropped_span_count too (egress.py). Deriving
    # the overflow figure by subtraction reported those foreign drops as this buffer overflowing.
    from tally.safety import SelfObservability

    obs = SelfObservability()
    t = _transport(_ScriptedSender([SendResult(400)]), observability=obs, max_buffer=2)
    t.export({"n": 1})
    t.export({"n": 2})
    t.export({"n": 3})  # one real overflow
    obs.dropped_span_count += 7  # a co-tenant of this obs drops spans of its own
    t.flush_once()  # refused: 2 spans rejected
    assert t.shed_counts() == {
        "undelivered_span_count": 0,
        "rejected_span_count": 2,
        "buffer_overflow_span_count": 1,
    }
