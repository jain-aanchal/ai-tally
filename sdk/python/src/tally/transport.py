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
- **Buffering + bounded retry, then shed and count.** A *retryable* failure (transport error, 429,
  any 5xx) resends the exact same bytes, so ``batch_id`` is stable and the gateway's
  ``(tenant_id, batch_id)`` idempotency key turns the resend into a replay rather than a duplicate.
  The wait honors the gateway's own hint (``Retry-After``, or ``server_hints.retry_after_ms``, which
  is what a 503 ``status: retry`` carries) clamped to the backoff ceiling, else capped exponential
  backoff + jitter. The attempt count is bounded by ``retry_max``; an exhausted batch is shed and
  COUNTED (``undelivered_span_count``), never retried forever and never silently forgotten. A
  *non-retryable* refusal (400, 401, 403, 422) is terminal on the first answer: resending identical
  bytes would only buy a second refusal, so the batch is shed and counted as
  ``rejected_span_count``. This mirrors ``infra/edge-proxy/internal/telemetry/telemetry.go``, which
  is the reference implementation of the pattern (CTO-36, #315).
- **Drains on shutdown.** :meth:`flush` and an ``atexit`` hook drain with a bounded timeout so a
  short-lived script still ships its spans.

Where these clients deliberately differ from the proxy (the authoritative list, kept here rather
than only in a PR description so it stays true as the code moves, #315 review):

===========================  ==============================  ====================================
divergence                   edge proxy                      SDK / backfill
===========================  ==============================  ====================================
retry budget                 4 attempts                      SDK ``retry_max`` (5); the backfill
                                                             61, a one-shot corpus load with no
                                                             hot path behind it
non-retryable refusal        shed, worker continues          SDK sheds and continues; the backfill
                                                             aborts, since a CLI has an operator
                                                             who can fix the credential, and a
                                                             re-run at the same seed is idempotent
hint clamp                   ``Max: 2 * time.Second``        SDK clamps to ``BackoffPolicy.max_ms``
                                                             (30s by default) so one policy object
                                                             governs every wait; the wait sits on
                                                             ``_stop.wait``, so stop()/atexit still
                                                             interrupt it and ``export()`` is never
                                                             blocked by it
backoff jitter               none                            SDK jitters +/- 25% via
                                                             ``BackoffPolicy``; the backfill now
                                                             jitters to match
body retry hint              header only                     both clients also read
                                                             ``server_hints.retry_after_ms``,
                                                             which is the only place the gateway's
                                                             503 shed states its wait
===========================  ==============================  ====================================

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
from dataclasses import dataclass

from tally.egress import BackoffPolicy
from tally.hmac_keys import HmacKeyBootstrap
from tally.safety import SelfObservability, safe_block
from tally.wire import BatchRequest, encode_request

_log = logging.getLogger("tally")

DEFAULT_ENDPOINT = "https://ingest.ai-tally.com"

@dataclass(frozen=True, slots=True)
class SendResult:
    """One POST's answer: the status, plus how long the gateway asked us to wait before a resend.

    ``retry_after_ms`` is ``None`` when the gateway named no delay and the client falls back to its
    own backoff. ``0`` is a real, distinct value: the gateway's overload shed sends
    ``server_hints.retry_after_ms = 0`` (gateway/backpressure.py) meaning "retry, we have no
    specific delay for you", not "hammer us". The client answers a 0 with its own bounded backoff,
    which is the same treatment the edge proxy gives an absent Retry-After.
    """

    status: int
    retry_after_ms: int | None = None


#: Sends one POST. Returns a :class:`SendResult`, or a bare status code (the older shape, still
#: accepted so an injected test sender or a caller's own sender keeps working); raises on a
#: network-level failure. Injectable.
Sender = Callable[[str, dict[str, str], bytes], "int | SendResult"]

#: Bounds how much of an ingest error body is read to find a retry hint. The ack is a small JSON
#: object; the cap only stops a misconfigured endpoint that streams from making us buffer without
#: limit. Nothing from the body is logged or stored: only the integer hint is used.
_MAX_ACK_BYTES = 64 * 1024

#: How many further sheds of one cause pass before the log speaks about it again. A standing cause
#: (a rotated ingest key answering 401) sheds a batch per flush for as long as the app runs, so the
#: first one warns in full and the rest are rolled into one line per this many. Counted by sheds
#: rather than by elapsed time so the damping is deterministic and testable, and because a busy app
#: and an idle one should both get the same number of lines per unit of loss (#315 review).
_SHED_LOG_EVERY = 100


def _retry_hint_ms(headers: object, body: bytes) -> int | None:
    """Extract the gateway's requested wait, preferring the header, then the body hint.

    ``Retry-After`` is read in its delay-seconds form, which is what the gateway's rate limiter and
    its overload shed both send (gateway/app.py). An HTTP-date form or garbage yields ``None`` and
    the caller falls back to its own backoff, so a malformed header can never stall the worker.
    """
    get = getattr(headers, "get", None)
    if callable(get):
        raw = get("Retry-After")
        if raw:
            try:
                secs = int(str(raw).strip())
            except ValueError:
                secs = -1
            if secs >= 0:
                return secs * 1000
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - a body we cannot parse simply carries no hint
        return None
    if not isinstance(parsed, dict):
        return None
    hints = parsed.get("server_hints")
    nested = hints.get("retry_after_ms") if isinstance(hints, dict) else None
    # A 429 states it at the top level, a 503 shed under server_hints (gateway/app.py).
    for candidate in (parsed.get("retry_after_ms"), nested):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


def _urllib_sender(
    url: str, headers: dict[str, str], body: bytes, *, timeout: float = 5.0
) -> SendResult:
    """Default POST sender over ``urllib`` (stdlib). Raises ``urllib.error.URLError`` on failure.

    urllib raises ``HTTPError`` for every non-2xx, but an HTTPError *is* the response, so the 429 /
    503 answers that carry a retry hint are read here rather than collapsing into a bare exception
    that loses the gateway's own instruction (#315).
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed endpoint
            return SendResult(int(resp.status))
    except urllib.error.HTTPError as err:
        try:
            ack = err.read(_MAX_ACK_BYTES)
        except Exception:  # noqa: BLE001 - a body we cannot read simply carries no hint
            ack = b""
        finally:
            err.close()
        return SendResult(int(err.code), _retry_hint_ms(err.headers, ack))


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
        # A batch pinned in flight across retries together with the EXACT bytes that were sent, so
        # every resend is byte-identical and batch_id is stable. Re-encoding per attempt would be
        # the bug that matters now that a duplicate span permanently pollutes the SummingMergeTree
        # rollups (#311): the gateway's (tenant_id, batch_id) idempotency store only recognizes a
        # replay if the replay is actually the same batch (#315). The bytes are None only in the
        # window between taking the batch off the buffer and encoding it outside the lock.
        self._pending: tuple[BatchRequest, bytes | None, int] | None = None
        # The gateway's own requested wait from the last retryable answer, or None for "it named
        # none, use our backoff". 0 is a real value and means "retry, no specific delay".
        self._retry_after_ms: int | None = None
        # Spans metered and then lost, kept apart because the two losses have different fixes: a
        # spent retry budget is an ingest availability problem, a refusal is a client-side one.
        self.undelivered_span_count = 0
        self.rejected_span_count = 0
        # Counted where it happens, in export(), not derived by subtracting the other two from
        # obs.dropped_span_count: obs can be shared with a BatchProcessor, which drops spans of its
        # own, and a subtracted figure would report those as this buffer overflowing (#315 review).
        self.buffer_overflow_span_count = 0
        # cause -> [sheds, spans] accumulated since that cause last spoke in the log.
        self._shed_log_state: dict[str, list[int]] = {}
        self._atexit_registered = False

    # --- Exporter protocol (hot path) ---
    def export(self, attributes: dict[str, object]) -> None:
        """Enqueue a span. Never blocks, never raises. Drops oldest on overflow (counted)."""
        with safe_block(self.obs, where="BatchingTransport.export"):
            with self._lock:
                if len(self._buf) >= self.max_buffer:
                    self._buf.popleft()
                    self.obs.dropped_span_count += 1
                    self.buffer_overflow_span_count += 1
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

    @staticmethod
    def _is_retryable(status: int) -> bool:
        """Backpressure and server faults are transient by definition; the same bytes are accepted
        once ingest recovers. Every other non-2xx is the gateway saying this batch is wrong (bad
        credential, wrong tenant, failed validation), and a resend only buys a second refusal. This
        is exactly the edge proxy's split (telemetry.go ``attempt``)."""
        return status == 429 or status >= 500

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
                    batch, body, attempts = self._pending
                else:
                    if not self._buf:
                        return False
                    n = min(self.max_batch_size, len(self._buf))
                    spans = [self._buf.popleft() for _ in range(n)]
                    batch = self._build_batch(spans)
                    body, attempts = None, 0
                    # Pinned with body=None, i.e. "taken, not yet encoded": pending() still counts
                    # these spans and a death before the encode cannot lose them.
                    self._pending = (batch, body, attempts)

            if body is None:
                # Encoded OUTSIDE _lock, deliberately. Serializing up to max_batch_size (512) spans
                # is not the "brief state transition" _lock exists for, and doing it under the lock
                # stalls every export() on the hot path once per flush, which the invariant above
                # forbids (CTO-260 §5, #315 review). Encoding stays exactly once per batch: the
                # bytes are pinned here and every attempt resends these same bytes, which is what
                # makes a resend a replay rather than a rollup-polluting duplicate (#311).
                body = encode_request(batch).encode("utf-8")
                with self._lock:
                    # Only re-pin our own batch. _flush_lock serializes flushes, so nothing can
                    # have replaced it, but a shed batch must never be resurrected by this write.
                    if self._pending is not None and self._pending[0] is batch:
                        self._pending = (batch, body, self._pending[2])

            headers = {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            }
            ok = False
            retryable = True  # a transport error is the most retryable failure there is
            status: int | None = None
            hint: int | None = None
            try:
                result = self._sender(self._url, headers, body)
                if isinstance(result, SendResult):
                    status, hint = result.status, result.retry_after_ms
                else:
                    status = int(result)
                ok = 200 <= status < 300
                retryable = ok or self._is_retryable(status)
            except Exception as exc:  # noqa: BLE001 - transport errors must never escape
                self.obs.record_error(exc, "BatchingTransport.flush")

            with self._lock:
                if ok:
                    self._pending = None
                    self._consecutive_failures = 0
                    self._retry_after_ms = None
                    return True

                attempts += 1
                self._consecutive_failures += 1
                self._retry_after_ms = hint
                if not retryable:
                    # Terminal on the first answer: shed and count, no resend. Counted apart from a
                    # spent budget because a 401 is fixed by the operator, not by waiting.
                    self.rejected_span_count += len(batch.resource_spans)
                    self._shed_locked(batch, f"refused with status {status}", f"status {status}")
                elif attempts >= self.retry_max:
                    self.undelivered_span_count += len(batch.resource_spans)
                    self._shed_locked(
                        batch, f"undelivered after {attempts} attempts", "spent retry budget"
                    )
                else:
                    # Keep the batch, and its bytes, pinned for an identical resend.
                    self._pending = (batch, body, attempts)
            return False

    def _shed_locked(self, batch: BatchRequest, why: str, cause: str) -> None:
        """Terminal loss of one batch. Counted and logged, never quietly forgotten and never
        reported as a success: an unshipped span is a real, visible number (CLAUDE.md, honest under
        uncertainty). ``cause`` is the stable key the log damping groups by, ``why`` the detail.
        Caller holds _lock."""
        lost = len(batch.resource_spans)
        self.obs.dropped_span_count += lost
        self.obs.record_error(
            RuntimeError(f"batch dropped: {why}"), "BatchingTransport.flush"
        )
        self._pending = None
        self._retry_after_ms = None
        # Warn, not debug: dropped spend is the one transport event an operator has to see. But a
        # standing cause (a rotated key answering 401) sheds one batch per flush indefinitely, so
        # an undamped warning here floods a busy app's log with the same line. First of each cause
        # speaks, then one rolled-up line per _SHED_LOG_EVERY further sheds of that cause;
        # shed_counts() remains the exact record (#315 review).
        seen = self._shed_log_state.get(cause)
        if seen is None:
            self._shed_log_state[cause] = [0, 0]
            _log.warning(
                "tally: shed %d span(s), batch %s (further sheds from this cause are summarized "
                "every %d; see shed_counts())",
                lost,
                why,
                _SHED_LOG_EVERY,
            )
            return
        seen[0] += 1
        seen[1] += lost
        if seen[0] >= _SHED_LOG_EVERY:
            _log.warning(
                "tally: shed %d more batch(es), %d span(s), still %s", seen[0], seen[1], cause
            )
            seen[0] = 0
            seen[1] = 0

    def shed_counts(self) -> dict[str, int]:
        """Spans this transport metered and could not ship, by cause. The honest total the caller
        needs to know a run lost data (#315).

        Every figure is tracked at its own site rather than derived by subtraction: ``obs`` is
        shared, and :class:`~tally.egress.BatchProcessor` also writes ``dropped_span_count``, so a
        subtracted overflow figure would silently absorb another component's drops (#315 review).
        """
        with self._lock:
            return {
                "undelivered_span_count": self.undelivered_span_count,
                "rejected_span_count": self.rejected_span_count,
                "buffer_overflow_span_count": self.buffer_overflow_span_count,
            }

    def current_backoff_ms(self) -> float:
        """Wait before the next attempt. The gateway's own hint wins when it named one, clamped to
        the backoff ceiling so a server asking for a five minute pause cannot stall the worker; a
        hinted 0 falls back to our jittered backoff, because "no specific delay" is not licence to
        hammer a gateway that is already shedding."""
        with self._lock:
            failures = self._consecutive_failures
            hint = self._retry_after_ms
        if hint is not None and hint > 0:
            return float(min(hint, self.backoff.max_ms))
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
