# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §5 — background batching transport: batching, retry, backpressure, drain, envelope."""

from __future__ import annotations

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
    # tenant omitted — the key decides at the gateway (CTO-260 §3.1).
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


def test_sender_error_never_raises():
    def boom(url, headers, body):
        raise RuntimeError("kaboom")

    t = _transport(boom, backoff=BackoffPolicy(base_ms=0, max_ms=0))
    t.export({"n": 1})
    # Must not raise; failure recorded to self-observability.
    assert t.flush_once() is False
    assert t.obs.internal_error_count >= 1
