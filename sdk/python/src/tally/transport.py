# SPDX-License-Identifier: Apache-2.0
"""Background batching ingest transport for ``tally.init`` (CTO-260 §5).

``init`` installs a :class:`BatchingTransport` as the client's ``Exporter``. Spans enqueue onto a
bounded in-memory buffer; a daemon worker thread flushes on ``flush_interval_s`` or when a size
threshold is reached, POSTing a :class:`~tally.wire.BatchRequest` to ``{endpoint}/v1/batches`` with
the ingest key as bearer. The design guarantees, all non-negotiable (CLAUDE.md, CTO-260 §5):

- **Never blocks the caller.** ``export`` only appends under a lock; no network I/O on the calling
  thread. A full buffer drops the oldest span and counts it (backpressure, drop-oldest).
- **Never raises.** Every path runs inside the safety boundary; a transport error is recorded to
  self-observability, never propagated.
- **Buffering + retry.** A failed flush retries the whole batch (idempotent by ``batch_id``) with
  capped exponential backoff + jitter, bounded by ``retry_max``; an exhausted batch is dropped with
  a counter, never retried forever.
- **Drains on shutdown.** :meth:`flush` and an ``atexit`` hook drain with a bounded timeout so a
  short-lived script still ships its spans.

The tenant is omitted from the envelope: the bearer key is authoritative and the gateway maps it to
the tenant (CTO-260 §3.1). The batch carries ``tenant_id=""`` so it claims no tenant.

HTTP uses the standard library only (the SDK keeps zero required runtime deps). The ``sender`` is
injectable so tests exercise batching, retry, and backpressure without a network or a live gateway.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable

from tally.egress import BackoffPolicy
from tally.hmac_keys import HmacKeyBootstrap
from tally.safety import SelfObservability, safe_block
from tally.wire import BatchRequest, encode_request

_log = logging.getLogger("tally")

DEFAULT_ENDPOINT = "https://ingest.ai-tally.com"

#: Sends one POST. Returns the HTTP status code; raises on a network-level failure. Injectable.
Sender = Callable[[str, dict[str, str], bytes], int]


def _urllib_sender(url: str, headers: dict[str, str], body: bytes, *, timeout: float = 5.0) -> int:
    """Default POST sender over ``urllib`` (stdlib). Raises ``urllib.error.URLError`` on failure."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint
        return int(resp.status)


def fetch_hmac_key(
    endpoint: str,
    key: str,
    *,
    opener: Callable[[str, dict[str, str], float], dict] | None = None,
    timeout: float = 5.0,
) -> HmacKeyBootstrap:
    """GET ``{endpoint}/v1/tenant/hmac-key`` under the ingest key and parse the bootstrap material.

    ``opener`` is injectable for tests: it takes ``(url, headers, timeout)`` and returns the parsed
    JSON body. The default reads over ``urllib``. The response body is never logged (CTO-260 §3.2).
    """
    url = f"{endpoint.rstrip('/')}/v1/tenant/hmac-key"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if opener is None:
        opener = _urllib_get_json
    body = opener(url, headers, timeout)
    import base64

    return HmacKeyBootstrap(
        tenant_id=str(body["tenant_id"]),
        key_version=str(body["key_version"]),
        material=base64.b64decode(body["key_material_b64"]),
        algorithm=str(body.get("algorithm", "HMAC-SHA256")),
    )


def _urllib_get_json(url: str, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read().decode("utf-8"))


class BatchingTransport:
    """Bounded buffer + background, retrying, backpressure-aware ingest exporter.

    Implements the ``Exporter`` protocol (``export(attributes)``) so it drops straight into
    :class:`~tally.client.TallyClient`.
    """

    def __init__(
        self,
        endpoint: str,
        key: str,
        *,
        sdk_version: str,
        sender: Sender | None = None,
        observability: SelfObservability | None = None,
        max_buffer: int = 10_000,
        max_batch_size: int = 512,
        flush_interval_s: float = 1.0,
        backoff: BackoffPolicy | None = None,
        retry_max: int = 5,
        timeout_s: float = 5.0,
    ) -> None:
        self.obs = observability or SelfObservability()
        self._endpoint = endpoint.rstrip("/")
        self._url = f"{self._endpoint}/v1/batches"
        self._key = key
        self._sdk_version = sdk_version
        self._sender: Sender = sender or (
            lambda u, h, b: _urllib_sender(u, h, b, timeout=timeout_s)
        )
        self.max_buffer = max_buffer
        self.max_batch_size = max_batch_size
        self.flush_interval_s = flush_interval_s
        self.backoff = backoff or BackoffPolicy()
        self.retry_max = retry_max

        self._buf: deque[dict[str, object]] = deque()
        # _lock guards every read and write of _buf, _pending and _consecutive_failures. It is held
        # only for brief state transitions, never across the network send, so export() on the hot
        # path is never blocked by an in-flight flush (CTO-260 §5).
        self._lock = threading.Lock()
        # _flush_lock serializes flush_once so the daemon worker and a concurrent flush() cannot run
        # two sends at once. Without it they race on _pending and either double-send a batch or drop
        # an already-dequeued batch's spans (CTO-260 §5, review finding).
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0
        # A batch pinned in flight across retries, so its batch_id is stable (idempotent resend).
        self._pending: tuple[BatchRequest, int] | None = None
        self._atexit_registered = False

    # --- Exporter protocol (hot path) ---
    def export(self, attributes: dict[str, object]) -> None:
        """Enqueue a span. Never blocks, never raises. Drops oldest on overflow (counted)."""
        with safe_block(self.obs, where="BatchingTransport.export"):
            with self._lock:
                if len(self._buf) >= self.max_buffer:
                    self._buf.popleft()
                    self.obs.dropped_span_count += 1
                self._buf.append(attributes)

    def pending(self) -> int:
        # Both reads are under the lock, and every write to _pending is too, so the ternary cannot
        # observe _pending flip to None between the check and the subscript (the TOCTOU that used to
        # raise TypeError and kill the daemon worker outside safe_block).
        with self._lock:
            extra = 0 if self._pending is None else len(self._pending[0].resource_spans)
            return len(self._buf) + extra

    # --- envelope ---
    def _build_batch(self, spans: list[dict[str, object]]) -> BatchRequest:
        # tenant_id="" - the bearer key decides the tenant at the gateway (CTO-260 §3.1).
        return BatchRequest(tenant_id="", sdk_version=self._sdk_version, resource_spans=spans)

    def flush_once(self) -> bool:
        """Flush a single batch. Returns True on delivery, False on empty/failure. Never raises.

        Serialized by _flush_lock so a concurrent daemon flush and a caller flush() never send two
        batches at once or race on _pending; the buffer pop and every _pending/_consecutive_failures
        transition happen under _lock, so no span is lost and no batch is double-sent (CTO-260 §5).
        """
        with self._flush_lock:
            # Assemble or reclaim the in-flight batch, then pin it before the send so a mid-send
            # failure (even a thread death) can never lose the already-dequeued spans.
            with self._lock:
                if self._pending is not None:
                    batch, attempts = self._pending
                else:
                    if not self._buf:
                        return False
                    n = min(self.max_batch_size, len(self._buf))
                    spans = [self._buf.popleft() for _ in range(n)]
                    batch = self._build_batch(spans)
                    attempts = 0
                    self._pending = (batch, attempts)

            headers = {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            }
            body = encode_request(batch).encode("utf-8")
            try:
                status = self._sender(self._url, headers, body)
                ok = 200 <= status < 300
            except Exception as exc:  # noqa: BLE001 - transport errors must never escape
                self.obs.record_error(exc, "BatchingTransport.flush")
                ok = False

            with self._lock:
                if ok:
                    self._pending = None
                    self._consecutive_failures = 0
                    return True

                # Failure: keep the batch pinned for retry, bounded by retry_max (idempotent resend
                # by batch_id).
                attempts += 1
                self._consecutive_failures += 1
                if attempts >= self.retry_max:
                    self.obs.dropped_span_count += len(batch.resource_spans)
                    self.obs.record_error(
                        RuntimeError(f"batch dropped after {attempts} attempts"),
                        "BatchingTransport.flush",
                    )
                    self._pending = None
                else:
                    self._pending = (batch, attempts)
            return False

    def current_backoff_ms(self) -> float:
        with self._lock:
            failures = self._consecutive_failures
        return self.backoff.delay_ms(failures)

    # --- background loop ---
    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._atexit_registered:
            atexit.register(self._atexit_drain)
            self._atexit_registered = True

        def _run() -> None:
            while not self._stop.is_set():
                delivered = self.flush_once()
                if delivered:
                    wait_s = self.flush_interval_s
                else:
                    backoff_ms = self.current_backoff_ms()
                    wait_s = (backoff_ms / 1000.0) if backoff_ms > 0 else self.flush_interval_s
                self._stop.wait(timeout=max(wait_s, 0.001))
            # Best-effort drain on stop.
            while self.pending() and self.flush_once():
                pass

        self._thread = threading.Thread(target=_run, name="tally-ingest", daemon=True)
        self._thread.start()

    def flush(self, timeout: float = 5.0) -> None:
        """Drain the buffer synchronously with a bounded time budget. Never raises."""
        import time

        deadline = time.monotonic() + timeout
        with safe_block(self.obs, where="BatchingTransport.flush_drain"):
            while self.pending() and time.monotonic() < deadline:
                if not self.flush_once():
                    # A failing gateway: back off briefly rather than spin the deadline away.
                    delay = min(self.current_backoff_ms() / 1000.0, 0.1)
                    if delay > 0:
                        time.sleep(delay)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _atexit_drain(self) -> None:
        with safe_block(self.obs, where="BatchingTransport.atexit"):
            self.flush(timeout=2.0)
            self.stop(timeout=2.0)
