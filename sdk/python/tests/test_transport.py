# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §5 - background batching transport: batching, retry, backpressure, drain, envelope."""

from __future__ import annotations

import ast
import logging
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from tally.egress import BackoffPolicy
from tally.transport import (
    _FLAG_ITEM_CODES,
    _MAX_ACK_BYTES,
    _MAX_CODE_LEN,
    _MAX_HINT_BYTES,
    _MAX_SUMMARY_CODES,
    _MAX_SUMMARY_LEN,
    _NON_ITEM_CODES,
    _NON_RETRYABLE_ITEM_CODES,
    _RETRYABLE_ITEM_CODES,
    BatchingTransport,
    SendResult,
    _code_summary,
    _parse_ack,
    _retry_hint_ms,
    _safe_code,
)
from tally.wire import BatchRequest, decode_request


class _Collector(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.WARNING)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@contextmanager
def caplog_at_warning():
    """Collect the tally logger's WARNING records without mutating global logging config."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("tally")
    handler = _Collector(records)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


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
        "rejected_by_gateway_span_count": 0,
        "unmapped_retryable_span_count": 0,
        "buffer_overflow_span_count": 0,
        "unknown_code_span_count": 0,
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
        "rejected_by_gateway_span_count": 0,
        "unmapped_retryable_span_count": 0,
        "buffer_overflow_span_count": 1,
        "unknown_code_span_count": 0,
    }


# --- CTO-391: spans the gateway rejects INSIDE a 200 ---


def _ack(
    accepted: int,
    errors: tuple = (),
    *,
    hints: dict | None = None,
    message: str = "shed under load",
) -> bytes:
    """The gateway's ack body (gateway/app.py ``_response_dict``), as bytes on the wire.

    ``message`` is the gateway's own text for the shed. It is a parameter because the ack's SIZE is
    load-bearing for the read cap: the buffered path says "ingest buffer at capacity; retry", which
    is what pushes a full-batch shed past 64 KiB (CTO-391 review).
    """
    import json

    return json.dumps(
        {
            "batch_id": "b-1",
            "status": "partial" if errors else "accepted",
            "accepted_spans": accepted,
            "partial_errors": [
                {"item_id": item_id, "code": code, "message": message}
                for item_id, code in errors
            ],
            "server_hints": {
                "flush_interval_ms": 5_000,
                "max_batch_size": 1_000,
                "sample_rate_override": None,
                "retry_after_ms": 0,
                **(hints or {}),
            },
            "replayed": False,
        }
    ).encode("utf-8")


class _StubGateway:
    """A stub gateway that admits the first ``keep`` items of every batch and sheds the rest as
    RATE_LIMITED inside a 200, which is exactly what backpressure does (gateway/app.py)."""

    def __init__(self, keep: int) -> None:
        self.keep = keep
        self.batches: list[list[int]] = []

    def __call__(self, url: str, headers: dict, body: bytes) -> SendResult:
        spans = decode_request(body.decode("utf-8")).resource_spans
        self.batches.append([int(s["n"]) for s in spans])
        shed = tuple((f"#{i}", "RATE_LIMITED") for i in range(self.keep, len(spans)))
        return SendResult(200, None, _ack(min(self.keep, len(spans)), shed))


def test_rate_limited_items_in_a_200_are_requeued_and_land_on_the_next_flush():
    # The reproduction: a 4-span batch, 2 accepted, 2 shed as RATE_LIMITED inside a 200. Before
    # CTO-391 this read as a clean success and two spans of billable spend vanished.
    gw = _StubGateway(keep=2)
    t = _transport(gw, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    for i in range(4):
        t.export({"n": i})
    assert t.flush_once() is True
    assert gw.batches[0] == [0, 1, 2, 3]
    assert t.pending() == 2  # exactly the shed spans are held, not the whole batch
    assert t.requeued_span_count == 2

    assert t.flush_once() is True
    # Exactly the rejected spans go again: an accepted span is never sent twice.
    assert gw.batches[1] == [2, 3]
    assert t.pending() == 0
    assert t.shed_counts() == {
        "undelivered_span_count": 0,
        "rejected_span_count": 0,
        "rejected_by_gateway_span_count": 0,
        "unmapped_retryable_span_count": 0,
        "buffer_overflow_span_count": 0,
        "unknown_code_span_count": 0,
    }


def test_partial_ack_warns_once_and_paces_the_resend():
    gw = _StubGateway(keep=1)
    t = _transport(gw, backoff=BackoffPolicy(base_ms=250, max_ms=1_000, jitter=0.0))
    for i in range(3):
        t.export({"n": i})
    with caplog_at_warning() as records:
        t.flush_once()
    assert len(records) == 1
    message = records[0].getMessage()
    assert "accepted 1 of 3" in message
    assert "RATE_LIMITED=2" in message
    assert "2 re-enqueued" in message
    # A delivered-but-partial flush still owes a wait: the gateway just said it is shedding.
    assert t.current_backoff_ms() == 250


def test_permanently_invalid_items_are_counted_and_never_retried():
    sender = _ScriptedSender([SendResult(200, None, _ack(1, (("#1", "PII_DETECTED"),)))])
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})
    assert t.flush_once() is True
    assert t.pending() == 0  # a resend would only buy a second refusal
    assert t.shed_counts()["rejected_by_gateway_span_count"] == 1
    assert t.shed_counts()["undelivered_span_count"] == 0
    assert t.requeued_span_count == 0
    assert t.obs.dropped_span_count == 1
    t.flush_once()
    assert len(sender.calls) == 1  # nothing was re-sent


def test_unknown_feature_tag_is_a_flag_not_a_loss():
    # Accepted-but-flagged: the span landed. Counting it as rejected would invent a loss.
    sender = _ScriptedSender([SendResult(200, None, _ack(2, (("#0", "UNKNOWN_FEATURE_TAG"),)))])
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})
    with caplog_at_warning() as records:
        assert t.flush_once() is True
    assert records == []
    assert t.pending() == 0
    assert t.requeued_span_count == 0
    assert t.shed_counts() == {
        "undelivered_span_count": 0,
        "rejected_span_count": 0,
        "rejected_by_gateway_span_count": 0,
        "unmapped_retryable_span_count": 0,
        "buffer_overflow_span_count": 0,
        "unknown_code_span_count": 0,
    }


def test_clean_200_clears_the_batch_with_no_warning():
    sender = _ScriptedSender([SendResult(200, None, _ack(2))])
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})
    with caplog_at_warning() as records:
        assert t.flush_once() is True
    assert records == []
    assert t.pending() == 0
    assert t.requeued_span_count == 0
    assert all(v == 0 for v in t.shed_counts().values())


def test_endless_shedding_is_bounded_and_counted_not_retried_forever():
    # A gateway that accepts nothing must not make the client trade the same spans back and forth
    # forever: the re-enqueue is bounded by retry_max and the remainder is counted, honestly.
    gw = _StubGateway(keep=0)
    t = _transport(gw, retry_max=3, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"n": 0})
    for _ in range(10):
        t.flush_once()
    assert t.pending() == 0
    assert len(gw.batches) == 4  # 1 send + 3 bounded resends
    assert t.shed_counts()["undelivered_span_count"] == 1


def test_no_span_content_or_account_id_reaches_any_log_record(caplog):
    import logging

    secret_account = "acct_live_CUSTOMER_4KZ"
    sender = _ScriptedSender(
        [SendResult(200, None, _ack(0, (("#0", "PII_DETECTED"), ("#1", "RATE_LIMITED"))))]
    )
    t = _transport(sender)
    t.export({"gen_ai.system": "openai", "gen_ai.tally.account_id_hash": secret_account, "n": 0})
    t.export({"gen_ai.system": "anthropic", "n": 1})
    with caplog.at_level(logging.DEBUG):
        t.flush_once()
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob  # the warning did fire, so this is a real assertion and not a vacuous one
    for leak in (secret_account, "openai", "anthropic", "account_id_hash", "#0", "item_id"):
        assert leak not in blob


def test_server_hints_lower_the_batch_ceiling_but_never_raise_it():
    overloaded = _ScriptedSender([SendResult(200, None, _ack(1, hints={"max_batch_size": 250}))])
    t = _transport(overloaded, max_batch_size=512)
    t.export({"n": 0})
    t.flush_once()
    assert t.max_batch_size == 250  # the gateway asked us to send less; obeyed
    assert t.last_server_hints["max_batch_size"] == 250

    healthy = _ScriptedSender([SendResult(200, None, _ack(1, hints={"max_batch_size": 1_000}))])
    t2 = _transport(healthy, max_batch_size=512)
    t2.export({"n": 0})
    t2.flush_once()
    # A hint is a ceiling, not a target: it must not enlarge the caller's deliberate 512.
    assert t2.max_batch_size == 512


def test_unreadable_ack_degrades_safely():
    # Malformed, empty and absent bodies must never raise and must never invent a loss.
    sender = _ScriptedSender(
        [SendResult(200, None, b"<html>gateway behind a proxy"), SendResult(200, None, b""), 200]
    )
    t = _transport(sender)
    for i in range(3):
        t.export({"n": i})
        assert t.flush_once() is True
    assert t.pending() == 0
    assert all(v == 0 for v in t.shed_counts().values())


def test_a_span_named_by_trace_id_is_the_span_that_goes_again():
    # Behavioural, through a real flush: this used to assert only on _item_positions' return dict,
    # with no transport involved, so it proved nothing about which span comes back (CTO-391 review).
    # The gateway names a span by trace:span when it has one, and numbers items AFTER intra-batch
    # dedup, so numbering the raw list would re-enqueue the WRONG span.
    sent: list[list[int]] = []

    def gw(url: str, headers: dict, body: bytes) -> SendResult:
        spans = decode_request(body.decode("utf-8")).resource_spans
        sent.append([int(s["n"]) for s in spans])
        if len(sent) == 1:
            return SendResult(200, None, _ack(2, (("t2:s2", "RATE_LIMITED"),)))
        return SendResult(200, None, _ack(len(spans)))

    t = _transport(gw, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"trace_id": "t1", "span_id": "s1", "n": 0})
    t.export({"trace_id": "t1", "span_id": "s1", "n": 1})  # duplicate ids: deduped before numbering
    t.export({"trace_id": "t2", "span_id": "s2", "n": 2})
    assert t.flush_once() is True
    assert sent[0] == [0, 1, 2]
    assert t.pending() == 1
    assert t.flush_once() is True
    # Exactly the named span, not the one that sits at the same raw index.
    assert sent[1] == [2]


def test_only_the_refused_spans_go_again_and_they_go_as_a_fresh_batch():
    # A behavioural regression test that survives the signature (CTO-391 review): most of the
    # CTO-391 tests fail on a revert only through TypeError, because the old SendResult had no body
    # field, which catches deletion but not damage. This one catches a plausible WRONG fix that
    # keeps every signature: re-enqueueing by leaving the batch PINNED. That would resend the
    # accepted spans as well, and resend them under the SAME batch_id, which the gateway's
    # (tenant_id, batch_id) idempotency store answers as a replay, so the refused spans would be
    # silently dropped a second time while the client believed it had recovered them.
    delivered: list[tuple[str, list[int]]] = []

    def gw(url: str, headers: dict, body: bytes) -> SendResult:
        req = decode_request(body.decode("utf-8"))
        spans = [int(s["n"]) for s in req.resource_spans]
        delivered.append((req.batch_id, spans))
        shed = tuple((f"#{i}", "RATE_LIMITED") for i in range(2, len(spans)))
        return SendResult(200, None, _ack(min(2, len(spans)), shed))

    t = _transport(gw, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    for i in range(4):
        t.export({"n": i})
    assert t.flush_once() is True
    assert t.flush_once() is True

    first_id, first_spans = delivered[0]
    second_id, second_spans = delivered[1]
    assert first_spans == [0, 1, 2, 3]
    assert second_spans == [2, 3]  # only the refused ones
    # The accepted spans are never put on the wire twice: that is the double-counted spend.
    assert first_spans.count(0) + second_spans.count(0) == 1
    assert first_spans.count(1) + second_spans.count(1) == 1
    # And the resend is a NEW batch, not a replay of one the gateway has already recorded.
    assert first_id != second_id


# --- CTO-391 review: an ack bigger than the read cap ---


def _shed_ack_bytes(n_spans: int) -> bytes:
    """An ack shedding a whole ``n_spans`` batch, sized as it really is on the wire: every item
    named with a 32-hex trace id and a 16-hex span id, carrying the gateway's own message text."""
    errors = tuple((f"{'a' * 32}:{'b' * 16}", "RATE_LIMITED") for _ in range(n_spans))
    return _ack(0, errors, message="ingest buffer at capacity; retry")


def test_the_ack_read_cap_fits_a_whole_default_batch_worth_of_sheds():
    # The SDK's default max_batch_size is 512. A gateway shedding all of them names every item, and
    # that ack measures ~70 KB: over the 64 KiB the client used to read. The cap truncated exactly
    # the ack that says spans were lost (CTO-391 review).
    body = _shed_ack_bytes(512)
    assert len(body) > 64 * 1024
    assert len(body) <= _MAX_ACK_BYTES


def test_an_ack_that_hit_the_read_cap_is_counted_and_warned_not_cleared_silently():
    # A truncated body parses to nothing, which took the "we learned nothing" branch and cleared
    # the batch, silently reintroducing the exact loss CTO-391 fixes, at the default batch size.
    # Truncation is KNOWN: the gateway had more to say about this batch than we read.
    truncated = _shed_ack_bytes(512)[:_MAX_ACK_BYTES]
    sender = _ScriptedSender([SendResult(200, None, truncated, True)])
    t = _transport(sender)
    for i in range(4):
        t.export({"n": i})
    with caplog_at_warning() as records:
        assert t.flush_once() is True
    assert t.pending() == 0  # not resent: the gateway may well have written some of them
    assert t.shed_counts()["rejected_by_gateway_span_count"] == 4
    assert t.obs.dropped_span_count == 4
    assert len(records) == 1
    assert "cannot be attributed" in records[0].getMessage()
    t.flush_once()
    assert len(sender.calls) == 1


def test_the_default_sender_flags_a_body_that_hit_the_cap(monkeypatch):
    # The flag has to come from the sender that actually reads the socket; without it the
    # accounting can never tell a short ack from one the cap cut short.
    import tally.transport as transport_mod

    class _Resp:
        status = 200

        def read(self, n: int) -> bytes:
            return b"x" * n  # always gives back everything asked for: an endless body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    result = transport_mod._urllib_sender("http://gw.test", {}, b"{}")
    assert result.body_truncated is True
    assert len(result.body) == _MAX_ACK_BYTES  # capped, and the cap is reported

    class _Small(_Resp):
        def read(self, n: int) -> bytes:
            return b"{}"

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", lambda req, timeout=None: _Small())
    assert transport_mod._urllib_sender("http://gw.test", {}, b"{}").body_truncated is False


# --- CTO-391 review: retryable sheds the gateway names under ids that are not ours ---


def test_buffer_overflow_sheds_are_counted_as_retryable_loss_not_permanent():
    # The buffered ingest path names its shed items "#buffer-overflow-N", numbered by the GATEWAY's
    # overflow position (gateway/app.py), so they match no id we sent and nothing can be put back.
    # They are still RATE_LIMITED: shed under load, retryable by contract. Booking them in the
    # permanent-loss counter sent an operator looking for bad client-side data when the real cause
    # was ingest capacity.
    errors = (("#buffer-overflow-0", "RATE_LIMITED"), ("#buffer-overflow-1", "RATE_LIMITED"))
    sender = _ScriptedSender([SendResult(200, None, _ack(2, errors))])
    t = _transport(sender)
    for i in range(4):
        t.export({"n": i})
    with caplog_at_warning() as records:
        assert t.flush_once() is True
    counts = t.shed_counts()
    assert counts["unmapped_retryable_span_count"] == 2
    assert counts["rejected_by_gateway_span_count"] == 0  # NOT a permanent refusal
    assert counts["undelivered_span_count"] == 0
    assert t.requeued_span_count == 0  # no id maps to a span, and none is guessed at
    blob = "\n".join(r.getMessage() for r in records)
    assert "retryable loss" in blob
    assert "buffer-overflow" not in blob  # item ids still never reach the log


# --- CTO-391 review: parsing an ack must never raise ---


def test_a_huge_hint_integer_neither_raises_nor_forces_a_resend():
    # float(raw_rate) sat outside the try that wraps json.loads, so a JSON integer of a few hundred
    # digits raised OverflowError out of _parse_ack. flush_once caught that as a send failure and
    # re-sent a batch the gateway had already accepted.
    body = b'{"accepted_spans": 1, "server_hints": {"sample_rate_override": ' + b"9" * 400 + b"}}"
    ack = _parse_ack(body)
    assert ack is not None
    assert ack.accepted_spans == 1
    assert ack.sample_rate_override is None  # unusable, so unknown; never a fabricated rate

    sender = _ScriptedSender([SendResult(200, None, body)])
    t = _transport(sender)
    t.export({"n": 0})
    assert t.flush_once() is True
    assert t.pending() == 0
    t.flush_once()
    assert len(sender.calls) == 1  # delivered once, not re-sent


# --- CTO-391 review: the no-double-send guarantee must not rest on the gateway being consistent ---


def test_a_self_inconsistent_ack_is_distrusted_rather_than_resending_an_accepted_span():
    # accepted_spans=4 on a 4-span batch that ALSO names #0 retryable cannot both be true. Acting
    # on the named position resent span 0, double-counting spend nothing downstream can undo.
    sent: list[list[int]] = []

    def gw(url: str, headers: dict, body: bytes) -> SendResult:
        sent.append([int(s["n"]) for s in decode_request(body.decode("utf-8")).resource_spans])
        return SendResult(200, None, _ack(4, (("#0", "RATE_LIMITED"),)))

    t = _transport(gw, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    for i in range(4):
        t.export({"n": i})
    with caplog_at_warning() as records:
        assert t.flush_once() is True
    assert sent == [[0, 1, 2, 3]]
    assert t.pending() == 0
    assert t.requeued_span_count == 0
    t.flush_once()
    assert sent == [[0, 1, 2, 3]]  # span 0 was NOT put on the wire a second time
    assert "distrusting" in records[0].getMessage()


# --- CTO-391 review: the buffer cap discards re-enqueued spans first ---


class _RefillingGateway:
    """Sheds every item as RATE_LIMITED, and refills the client's buffer to its cap mid-send.

    Models the real race: spans keep arriving on the hot path while a batch is in flight, so the
    re-enqueue lands on a buffer that is already full.
    """

    def __init__(self, refill: int) -> None:
        self.refill = refill
        self.transport: BatchingTransport | None = None

    def __call__(self, url: str, headers: dict, body: bytes) -> SendResult:
        spans = decode_request(body.decode("utf-8")).resource_spans
        assert self.transport is not None
        for j in range(self.refill):
            self.transport.export({"n": 100 + j})
        shed = tuple((f"#{i}", "RATE_LIMITED") for i in range(len(spans)))
        return SendResult(200, None, _ack(0, shed))


def test_requeued_spans_the_buffer_cap_discards_are_counted_and_logged_honestly():
    # _requeue_locked inserts at the front and trims with popleft, so the re-enqueued spans are the
    # very first thing the cap throws away, and requeued_span_count had already counted all of them.
    # The warning read "4 re-enqueued, 0 lost" about four spans that were gone (CTO-391 review).
    gw = _RefillingGateway(refill=4)
    t = _transport(gw, max_buffer=4)
    gw.transport = t
    for i in range(4):
        t.export({"n": i})
    with caplog_at_warning() as records:
        assert t.flush_once() is True

    assert t.requeued_span_count == 0  # none of them survived the cap
    assert t.buffer_overflow_span_count == 4
    message = records[0].getMessage()
    assert "0 re-enqueued" in message
    assert "4 dropped by the buffer cap" in message

    # And what is actually in the buffer is what arrived during the send, not the re-enqueued spans.
    survivors: list[list[int]] = []

    def capture(url: str, headers: dict, body: bytes) -> SendResult:
        spans = decode_request(body.decode("utf-8")).resource_spans
        survivors.append([int(s["n"]) for s in spans])
        return SendResult(200, None, _ack(len(spans)))

    t._sender = capture
    t.flush_once()
    assert survivors == [[100, 101, 102, 103]]


def test_the_partial_retry_bound_is_per_transport_not_per_span():
    """Pins the KNOWN LIMIT documented on ``_partial_retry_rounds`` (CTO-391 review).

    This test characterizes current behaviour rather than a fix. The budget counts consecutive
    partial flushes for the whole transport, so a span refused for the FIRST time during a shedding
    episode inherits a budget earlier spans spent, and is dropped after that single refusal and
    counted as though its own retries had run out. Making it per span means carrying a round count
    with every buffered span through the pinned-batch path that guarantees no span is sent twice,
    which is a larger change than this bug fix. The limit is pinned here so it is visible, and so a
    later per-span fix has an assertion to flip deliberately rather than discovering this by
    accident.
    """
    gw = _StubGateway(keep=0)
    t = _transport(gw, retry_max=3, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"n": 0})
    for _ in range(3):
        t.flush_once()  # span 0 spends the shared budget; it is still buffered
    assert t.shed_counts()["undelivered_span_count"] == 0

    t.export({"n": 999})  # a fresh span, never refused by anyone
    t.flush_once()
    assert t.pending() == 0
    # Both dropped, and the fresh span is booked against a retry budget it never had.
    assert t.shed_counts()["undelivered_span_count"] == 2


# --- CTO-391 review: the mirrored code set must not drift from the gateway's own ---


def _gateway_errors_py() -> Path:
    """Locate ``gateway/errors.py``, skipping ONLY when the gateway project is absent entirely.

    The skip used to be ``if not errors_py.exists()``, which is vacuous in the one case that
    matters: move, rename or delete errors.py inside this repo and the contract test guarding the
    wire contract passes, having checked nothing. So the skip now hangs off a marker for "is the
    gateway project here at all" (its pyproject), which is what distinguishes the SDK being tested
    standalone from an sdist. Marker present but errors.py missing is a FAILURE, because that is a
    real break of the mirror this test exists to hold (CTO-406).
    """
    gateway_root = Path(__file__).resolve().parents[3] / "infra" / "gateway"
    if not (gateway_root / "pyproject.toml").exists():
        pytest.skip("gateway project not in this checkout (SDK tested standalone)")
    errors_py = gateway_root / "src" / "gateway" / "errors.py"
    assert errors_py.exists(), (
        f"the gateway project is in this checkout but {errors_py} is missing, so the SDK's "
        "mirrored code sets cannot be checked against it"
    )
    return errors_py


def _gateway_code_sets() -> tuple[dict[str, str], dict[str, set[str]]]:
    """Read the gateway's codes textually: member -> wire string, plus each declared code set.

    Textual, via ast: there is no gateway import to be had (the SDK depends on no gateway code),
    and textual is enough to catch the failure that actually happens, which is somebody editing one
    set and not the other.
    """
    tree = ast.parse(_gateway_errors_py().read_text(encoding="utf-8"))
    wire_value: dict[str, str] = {}  # ErrorCode member -> the string that goes on the wire
    declared: dict[str, set[str]] = {}  # module-level set name -> the members it names
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ErrorCode":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    wire_value[stmt.targets[0].id] = stmt.value.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            declared[node.target.id] = {
                n.attr for n in ast.walk(node.value) if isinstance(n, ast.Attribute)
            }
    return wire_value, declared


def test_non_retryable_item_codes_match_the_gateways_own_set():
    """The SDK mirrors ``gateway/errors.py`` ``NON_RETRYABLE`` as a literal, because it depends on
    no gateway code. Nothing pinned the two together, so a reclassification on the gateway side
    would silently make this client retry an item that can never be accepted, or permanently drop
    one that would have been (CTO-391 review).
    """
    wire_value, declared = _gateway_code_sets()
    non_retryable_members = declared.get("NON_RETRYABLE")
    assert wire_value, "could not read ErrorCode from the gateway source"
    assert non_retryable_members, "could not read NON_RETRYABLE from the gateway source"
    assert {wire_value[m] for m in non_retryable_members} == set(_NON_RETRYABLE_ITEM_CODES)
    # And the accepted-but-flagged codes are real codes, not a spelling this client invented.
    assert set(_FLAG_ITEM_CODES) <= set(wire_value.values())


# --- CTO-407: the item-id mirror and the wire's own dedup must not drift apart ---


def _gateway_item_ids(spans: list[dict]) -> list[str]:
    """Number a batch the way the gateway does: ``span_item_id`` over ``deduplicated()``.

    Stated in the SDK's own terms, over the SDK's own ``wire.deduplicated()``, rather than imported
    from the gateway: this client depends on no gateway code, and the numbering rule itself
    (trace:span when both are present, else the post-dedup position) is two lines. What makes it a
    pin is the ``deduplicated()`` call, which is the very function ``_item_positions`` claims to
    mirror, so editing either rule alone fails the other.
    """
    deduped = BatchRequest(
        tenant_id="", sdk_version="0.0.1", resource_spans=list(spans)
    ).deduplicated()
    ids: list[str] = []
    for index, span in enumerate(deduped.resource_spans):
        trace = span.get("TraceId") or span.get("trace_id")
        span_id = span.get("SpanId") or span.get("span_id")
        ids.append(f"{trace}:{span_id}" if (trace and span_id) else f"#{index}")
    return ids


@pytest.mark.parametrize(
    "spans",
    [
        pytest.param([{"n": 0}, {"n": 1}, {"n": 2}], id="no_ids_at_all"),
        pytest.param(
            [{"span_id": "s1", "n": 0}, {"span_id": "s1", "n": 1}, {"n": 2}],
            id="duplicate_span_id_no_trace_id_the_cto_407_reproduction",
        ),
        pytest.param(
            [
                {"trace_id": "t1", "span_id": "s1", "n": 0},
                {"trace_id": "t1", "span_id": "s1", "n": 1},
                {"trace_id": "t1", "span_id": "s2", "n": 2},
            ],
            id="duplicate_full_ids",
        ),
        pytest.param(
            [
                {"trace_id": "t1", "span_id": "s1", "n": 0},
                {"trace_id": "t2", "span_id": "s1", "n": 1},
            ],
            id="same_span_id_different_traces_is_not_a_duplicate",
        ),
        pytest.param(
            [{"trace_id": "t1", "n": 0}, {"trace_id": "t1", "n": 1}],
            id="trace_id_only_is_never_deduped",
        ),
        pytest.param(
            [{"span_id": "", "n": 0}, {"span_id": "", "n": 1}],
            id="empty_span_id_is_not_an_identity",
        ),
        pytest.param(
            [{"span_id": "s1", "n": 0}, {"n": 1}, {"span_id": "s1", "n": 2}, {"n": 3}],
            id="a_dedup_in_the_middle_shifts_every_later_number",
        ),
    ],
)
def test_the_item_id_mirror_numbers_a_batch_exactly_as_the_wire_dedup_does(spans):
    """Pins ``_item_positions`` to ``wire.deduplicated()`` in both directions (CTO-407).

    The gateway numbers its ``partial_errors`` AFTER intra-batch dedup, so the two rules must drop
    the same spans in the same order. They did not: this client deduped only when both ids were
    truthy while the wire dedups on any real span id, and one extra item on the gateway's side
    shifts every ``#N`` after it onto the wrong span. Keys, not just values, and in order.
    """
    t = _transport(_RecordingSender())
    assert list(t._item_positions(spans)) == _gateway_item_ids(spans)


def test_a_shed_item_re_enqueues_the_shed_span_not_the_accepted_one():
    """The behaviour the mirror protects (CTO-407).

    Two spans share a span id and carry no trace id, so the gateway dedups one away and its ``#1``
    is the THIRD span we sent. Numbering that as our second span re-sent a span the gateway had
    already accepted, double-counting its spend, and left the span it actually shed uncounted.
    """
    sent_batches: list[list[int]] = []

    def sender(url: str, headers: dict, body: bytes) -> SendResult:
        spans = decode_request(body.decode("utf-8")).resource_spans
        sent_batches.append([int(s["n"]) for s in spans])
        if len(sent_batches) == 1:
            # Post-dedup the gateway holds [n=0, n=2] and sheds its own #1, which is n=2.
            return SendResult(200, None, _ack(1, (("#1", "RATE_LIMITED"),)))
        return SendResult(200, None, _ack(len(spans)))

    t = _transport(sender, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"span_id": "s1", "n": 0})
    t.export({"span_id": "s1", "n": 1})
    t.export({"n": 2})

    assert t.flush_once() is True
    assert sent_batches[0] == [0, 1, 2]
    assert t.requeued_span_count == 1
    assert t.flush_once() is True
    # Exactly the shed span goes again. The accepted span, and the duplicate the gateway dropped
    # before it ever numbered anything, do not.
    assert sent_batches[1] == [2]
    assert t.pending() == 0


# --- CTO-408: a gateway-supplied code reaches a log record, so it must be bounded and filtered ---


def test_a_hostile_item_code_cannot_forge_a_log_line(caplog):
    """The reproduction (CTO-408).

    A 300 character code carrying an embedded newline and the text "WARNING injected" produced a 491
    character record with a second physical line that reads exactly like a real warning. The
    attacker here is the endpoint, and the target is the customer's own log pipeline.
    """
    import logging

    hostile = "X" * 150 + "\nWARNING injected" + "X" * 150
    sender = _ScriptedSender([SendResult(200, None, _ack(1, (("#1", hostile),)))])
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})
    with caplog.at_level(logging.DEBUG):
        t.flush_once()

    messages = [r.getMessage() for r in caplog.records]
    assert messages  # the warning did fire, so this is a real assertion and not a vacuous one
    for message in messages:
        # One record is one line: no forged second line, however the record is later formatted.
        assert "\n" not in message
        assert "\r" not in message
        assert "WARNING injected" not in message
        assert "WARNINGinjected" not in message
        # And the record stays a readable size rather than carrying the endpoint's payload.
        assert len(message) < 400
    # The outcome is still reported, under a code that cannot be mistaken for a real one.
    assert any("_ALTERED" in m for m in messages)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("RATE_LIMITED", "RATE_LIMITED"),
        ("PAYLOAD_TOO_LARGE", "PAYLOAD_TOO_LARGE"),
        ("gen.ai-2", "gen.ai-2"),
        ("BAD\nCODE", "BADCODE_ALTERED"),
        ("BAD\r\nCODE", "BADCODE_ALTERED"),
        ("DROP\x00TABLE", "DROPTABLE_ALTERED"),
        ("\x1b[31mred", "31mred_ALTERED"),
        ("\x00\x1b\r\n", "UNPRINTABLE_CODE_ALTERED"),
        ("", "UNPRINTABLE_CODE_ALTERED"),
        # Line terminators Python's splitlines() honours but a plain "\n" check does not, so they
        # were unpinned before (CTO-408 review). U+2028, U+2029, U+0085.
        ("BAD CODE", "BADCODE_ALTERED"),
        ("BAD CODE", "BADCODE_ALTERED"),
        ("BAD\x85CODE", "BADCODE_ALTERED"),
    ],
)
def test_safe_code_keeps_a_plausible_code_and_strips_the_rest(raw, expected):
    assert _safe_code(raw) == expected


@pytest.mark.parametrize(
    "hostile",
    [
        "‮PII_DETECTED",  # a right-to-left override in front of a permanent-drop code
        "PII _DETECTED",  # a line separator inside one
        "PII\x85_DETECTED",  # a NEL inside one
        "PII_DETECTED\x00",
        "‮RATE_LIMITED",  # and the retryable side of the same trick
        "RATE _LIMITED",
        "RATE_LIMITED" + "!" * 100,  # 112 hostile characters that strip to exactly a real code
    ],
)
def test_filtering_can_never_turn_junk_into_a_real_code(hostile):
    """The regression this hygiene introduced (CTO-408 review).

    Stripping silently could rewrite garbage INTO a code this client classifies. Landing on
    PII_DETECTED means a permanent drop of billable spans, where an unrecognised code was merely
    retried before CTO-408 existed. Any altered string must therefore be visibly altered.
    """
    out = _safe_code(hostile)
    assert out != hostile  # the input really is hostile, so this case is not vacuous
    assert out.endswith("_ALTERED")
    assert out not in _NON_RETRYABLE_ITEM_CODES  # cannot be classified as a permanent refusal
    assert out not in _FLAG_ITEM_CODES  # nor silently swallowed as accepted-but-flagged
    assert len(out) <= _MAX_CODE_LEN


def test_an_altered_code_is_never_classified_as_a_permanent_drop(caplog):
    """Behavioural: the span comes back rather than being written off (CTO-408 review)."""
    import logging

    sender = _ScriptedSender(
        [SendResult(200, None, _ack(1, (("#1", "‮PII_DETECTED"),))), SendResult(200)]
    )
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})
    with caplog.at_level(logging.DEBUG):
        t.flush_once()

    # Retried, not booked as a permanent gateway refusal: the pre-CTO-408 fate of a code we do not
    # recognise. Reading PII_DETECTED out of that string would have lost the span for good.
    assert t.pending() == 1
    assert t.rejected_by_gateway_span_count == 0


def test_safe_code_is_bounded_however_long_the_input():
    cut = _safe_code("A" * 10_000)
    assert len(cut) == _MAX_CODE_LEN
    assert cut.endswith("_ALTERED")  # a cut code cannot pass as a real one


def test_truncation_is_decided_on_the_raw_length_not_the_stripped_one():
    """A long hostile code that strips short must still be marked (CTO-408 review).

    Deciding the cap on the filtered string let 112 characters of hostile input pass through as an
    unmarked, 12 character, perfectly real RATE_LIMITED.
    """
    raw = "RATE_LIMITED" + "!" * 100
    assert len(raw) > _MAX_CODE_LEN
    assert _safe_code(raw) == "RATE_LIMITED_ALTERED"


def test_every_code_this_client_knows_survives_sanitising_unchanged():
    """Sanitising must never rewrite a real code: that would silently reclassify an item."""
    for code in set(_NON_RETRYABLE_ITEM_CODES) | set(_FLAG_ITEM_CODES):
        assert _safe_code(code) == code
        assert len(code) <= _MAX_CODE_LEN


def test_a_hostile_code_is_bounded_in_state_not_only_in_the_log():
    """Sanitised at the parse boundary, so the oversized string is never held at all."""
    ack = _parse_ack(
        b'{"accepted_spans": 1, "partial_errors": '
        b'[{"item_id": "#0", "code": "' + b"Z" * 5_000 + b'"}]}'
    )
    assert ack is not None
    assert [len(code) for _, code in ack.errors] == [_MAX_CODE_LEN]


def test_the_joined_log_line_is_bounded_in_aggregate_not_only_per_code(caplog):
    """Bounding each code bounds nothing for the line as a whole (CTO-408 review).

    512 distinct 64 character codes sit well inside the 1 MiB ack read cap, so this is reachable in
    normal operation rather than a contrivance, and joining every distinct code produced a single
    34,990 character WARNING record. The earlier size assertion in this file passed only because its
    ack named ONE code, so it pinned nothing about the aggregate.
    """
    import logging

    n = 512
    errors = tuple((f"#{i}", f"C{i:03d}".ljust(_MAX_CODE_LEN, "Z")) for i in range(n))
    sender = _ScriptedSender([SendResult(200, None, _ack(0, errors)), SendResult(200)])
    t = _transport(sender)
    for i in range(n):
        t.export({"n": i})
    with caplog.at_level(logging.DEBUG):
        t.flush_once()

    messages = [r.getMessage() for r in caplog.records]
    partial = [m for m in messages if "inside a 200" in m]
    assert partial  # the line under test really did fire, so this is not a vacuous assertion
    for message in messages:
        assert len(message) < 600  # the aggregate bound, with every one of the 512 codes in play
        assert "\n" not in message
    # The tail is counted rather than printed, so the line still says how much it is not naming.
    assert f"+{n - _MAX_SUMMARY_CODES} more" in partial[0]


def test_the_code_summary_names_the_biggest_offenders_and_counts_the_rest():
    counts = {"RATE_LIMITED": 3, "INVALID_SCHEMA": 9, "PII_DETECTED": 1}
    # Ranked by count, so the line names what actually happened rather than what sorts first.
    assert _code_summary(counts) == "INVALID_SCHEMA=9, RATE_LIMITED=3, PII_DETECTED=1"
    assert _code_summary({}) == ""

    many = {f"C{i:03d}": 1 for i in range(50)}
    summary = _code_summary(many)
    assert summary.count("=") == _MAX_SUMMARY_CODES
    assert summary.endswith(f"+{50 - _MAX_SUMMARY_CODES} more")
    assert len(summary) <= _MAX_SUMMARY_LEN


def test_an_oversized_body_is_not_json_parsed_for_a_retry_hint():
    """CTO-408, the second observation: a hint is a handful of bytes, so a megabyte is not read.

    Falling back to our own backoff is a state this function already returns for an absent hint, so
    nothing new is invented on the degraded path.
    """
    padding = b"x" * _MAX_HINT_BYTES
    huge = b'{"server_hints": {"retry_after_ms": 1000}, "pad": "' + padding + b'"}'
    assert _retry_hint_ms(None, huge) is None
    # The same hint in a body of sane size is still honoured.
    assert _retry_hint_ms(None, b'{"server_hints": {"retry_after_ms": 1000}}') == 1000


def test_the_flag_code_set_is_pinned_to_the_gateways_own_in_both_directions():
    """Equality, not containment (CTO-406).

    This used to assert only ``_FLAG_ITEM_CODES <= gateway codes``, i.e. that the flags this client
    knows are real codes. The direction that was missing is the one that bites: a flag code ADDED on
    the gateway satisfied that assertion perfectly while reaching this client as an unknown code,
    and an unknown code was treated as retryable. Since the gateway counts an accepted-but-flagged
    span in ``accepted_spans``, that made ``accepted + named`` exceed the batch, tripped the
    self-consistency guard, and turned the whole ack into "distrusted", which clears the batch on
    status alone. That is the CTO-391 silent loss, back behind a warning about distrust rather than
    about spans. Pinned both ways, adding a flag code to errors.py fails here until the SDK learns
    it.
    """
    wire_value, declared = _gateway_code_sets()
    flag_members = declared.get("ACCEPTED_BUT_FLAGGED")
    assert flag_members, "could not read ACCEPTED_BUT_FLAGGED from the gateway source"
    assert {wire_value[m] for m in flag_members} == set(_FLAG_ITEM_CODES)


def test_every_gateway_code_is_classified_somewhere_in_this_client():
    """No gateway code may reach this client unclassified (CTO-406).

    The partition is the general form of the trap: ``errors.py`` invites additive codes in its own
    header, and every code this client has not placed in one of its four sets becomes an "unknown"
    at runtime, in a customer's process, rather than a red test here. Adding ANY code to errors.py
    now fails this test until somebody decides which set it belongs in.
    """
    wire_value, _ = _gateway_code_sets()
    classified = (
        set(_NON_RETRYABLE_ITEM_CODES)
        | set(_FLAG_ITEM_CODES)
        | set(_RETRYABLE_ITEM_CODES)
        | set(_NON_ITEM_CODES)
    )
    assert set(wire_value.values()) == classified


# --- CTO-406: one unrecognised code must not invalidate a whole ack ---


def test_a_new_accepted_but_flagged_code_does_not_switch_the_accounting_off():
    """The reproduction. A code the SDK has never seen, on a span the gateway ACCEPTED.

    Before: unknown meant retryable, so the span was re-enqueued and counted, ``accepted + named``
    came to 3 for a 2 span batch, the self-consistency guard fired, and the ack was distrusted
    whole, clearing the batch on status alone.
    """
    sender = _ScriptedSender([SendResult(200, None, _ack(2, (("#0", "DEPRECATED_MODEL"),)))])
    t = _transport(sender)
    t.export({"n": 0})
    t.export({"n": 1})

    with caplog_at_warning() as records:
        assert t.flush_once() is True

    messages = " ".join(r.getMessage() for r in records)
    assert "distrusting" not in messages  # the ack's own numbers add up fine
    assert "does not recognise" in messages
    assert t.pending() == 0  # never resent: it may well have landed
    assert t.requeued_span_count == 0
    assert t.unknown_code_span_count == 1
    assert t.shed_counts()["unknown_code_span_count"] == 1
    # Not booked as a loss, because it is not known to be one.
    assert t.obs.dropped_span_count == 0
    assert t.shed_counts()["rejected_by_gateway_span_count"] == 0


def test_an_unknown_code_does_not_disturb_the_known_outcomes_beside_it():
    """One unknown code must cost only its own span's certainty, not the rest of the ack."""
    sender = _ScriptedSender(
        [
            SendResult(
                200, None, _ack(1, (("#1", "RATE_LIMITED"), ("#2", "SOME_FUTURE_CODE")))
            ),
            SendResult(200, None, _ack(1)),
        ]
    )
    t = _transport(sender, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    for i in range(3):
        t.export({"n": i})

    assert t.flush_once() is True
    # The known retryable span still goes back on the buffer, exactly as before.
    assert t.pending() == 1
    assert t.requeued_span_count == 1
    assert t.unknown_code_span_count == 1
    # And no loss is invented for either of them.
    assert t.shed_counts()["rejected_by_gateway_span_count"] == 0
    assert t.shed_counts()["undelivered_span_count"] == 0

    assert t.flush_once() is True
    assert decode_request(sender.calls[1][2].decode("utf-8")).resource_spans == [{"n": 1}]


def test_a_known_retryable_code_other_than_rate_limited_still_goes_again():
    """Pins the retryable set as a set, not as "whatever is left over".

    ``QUOTA_EXCEEDED`` and ``IDEMPOTENCY_UNAVAILABLE`` are retryable on the gateway's own terms
    (errors.py), and naming them explicitly is what lets an unrecognised code be treated as
    unrecognised rather than as a retry (CTO-406).
    """
    sender = _ScriptedSender(
        [
            SendResult(200, None, _ack(1, (("#1", "QUOTA_EXCEEDED"),))),
            SendResult(200, None, _ack(1)),
        ]
    )
    t = _transport(sender, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"n": 0})
    t.export({"n": 1})

    assert t.flush_once() is True
    assert t.pending() == 1
    assert t.requeued_span_count == 1
    assert t.unknown_code_span_count == 0
    assert t.flush_once() is True
    assert decode_request(sender.calls[1][2].decode("utf-8")).resource_spans == [{"n": 1}]
