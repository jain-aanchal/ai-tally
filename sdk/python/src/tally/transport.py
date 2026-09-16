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
- **A 200 is not a receipt for every span.** The gateway answers HTTP 200 with ``status: partial``
  when only SOME items were refused, naming each refusal in ``partial_errors``; only an all-refused
  batch becomes a 422 (gateway/app.py). A bare status check therefore hides the losses that matter,
  which is how a shedding gateway could drop billable spend from a 512-span batch while this
  transport reported success (CTO-391). So every 2xx ack is read: items refused with a RETRYABLE
  code (``RATE_LIMITED``, what backpressure sheds) go back on the buffer and ship on a later flush,
  items refused PERMANENTLY (``PII_DETECTED``, ``INVALID_SCHEMA``, ``PAYLOAD_TOO_LARGE``) are
  counted in ``rejected_by_gateway_span_count`` and warned about rather than resent, and
  ``UNKNOWN_FEATURE_TAG`` is an accepted-but-flagged marker, so it counts as neither.
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
in-200 item rejections       counted into ``Stats.Rejected`` SDK counts them too, and additionally
                             (one span per batch, so there   re-enqueues the ones whose code is
                             is nothing to re-enqueue)       retryable (CTO-391)
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
    """One POST's answer: the status, the gateway's requested wait, and the ack body.

    ``retry_after_ms`` is ``None`` when the gateway named no delay and the client falls back to its
    own backoff. ``0`` is a real, distinct value: the gateway's overload shed sends
    ``server_hints.retry_after_ms = 0`` (gateway/backpressure.py) meaning "retry, we have no
    specific delay for you", not "hammer us". The client answers a 0 with its own bounded backoff,
    which is the same treatment the edge proxy gives an absent Retry-After.

    ``body`` carries the ack bytes, including on a 2xx, because the per-item outcomes that decide
    what actually landed live only in the body (CTO-391). It is last and defaulted so every existing
    construction, ``SendResult(503, 0)`` included, keeps working unchanged; a sender that supplies
    nothing teaches this client nothing beyond the status, which is the pre-CTO-391 behaviour.
    """

    status: int
    retry_after_ms: int | None = None
    body: bytes = b""


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


#: Per-item codes that mean "this exact item will be refused again", mirroring the gateway's own
#: ``NON_RETRYABLE`` set (gateway/errors.py). Kept as a literal here because the SDK depends on no
#: gateway code, and read from that set rather than guessed so a code the gateway later reclassifies
#: is a one-line change in a named place (CTO-391). Anything outside this set and not a flag below
#: is treated as retryable, which is the contract backpressure's RATE_LIMITED relies on.
_NON_RETRYABLE_ITEM_CODES: frozenset[str] = frozenset(
    {
        "UNAUTHENTICATED",
        "FORBIDDEN_SCOPE",
        "TENANT_MISMATCH",
        "INVALID_SCHEMA",
        "PII_DETECTED",
        "PAYLOAD_TOO_LARGE",
    }
)

#: Codes the gateway reports on items it ACCEPTED (gateway/app.py: "accepted-but-flagged"). These
#: are advice, not loss: counting one as a rejected span would invent a loss that never happened.
_FLAG_ITEM_CODES: frozenset[str] = frozenset({"UNKNOWN_FEATURE_TAG"})


@dataclass(frozen=True, slots=True)
class _BatchAck:
    """The subset of the gateway's BatchResponse this client acts on (gateway/app.py
    ``_response_dict``). Deliberately partial: ``message`` is never decoded, because it can echo
    request detail and a telemetry failure must not become its own leak (CTO-391, and the same
    reasoning as the edge proxy's ``ingestAck``).

    ``accepted_spans`` is ``None`` when the ack did not state a usable count. That is "unknown",
    not "zero": the caller must not turn it into a loss figure (CLAUDE.md, honest under doubt).
    """

    accepted_spans: int | None = None
    errors: tuple[tuple[str, str], ...] = ()  # (item_id, code)
    max_batch_size: int | None = None
    sample_rate_override: float | None = None
    retry_after_ms: int | None = None


def _parse_ack(body: bytes) -> _BatchAck | None:
    """Parse a 2xx ack. Returns ``None`` when the body teaches us nothing. Never raises.

    Every field is validated on the way in, because a malformed or truncated ack must degrade to
    "we learned nothing" and leave the SDK's never-raise guarantee intact (CTO-391).
    """
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - an ack we cannot parse simply carries no detail
        return None
    if not isinstance(parsed, dict):
        return None

    accepted = parsed.get("accepted_spans")
    accepted_spans = (
        accepted
        if isinstance(accepted, int) and not isinstance(accepted, bool) and accepted >= 0
        else None
    )

    errors: list[tuple[str, str]] = []
    raw_errors = parsed.get("partial_errors")
    if isinstance(raw_errors, list):
        for entry in raw_errors:
            if not isinstance(entry, dict):
                continue
            item_id, code = entry.get("item_id"), entry.get("code")
            if isinstance(item_id, str) and isinstance(code, str) and code:
                errors.append((item_id, code))

    hints = parsed.get("server_hints")
    hints = hints if isinstance(hints, dict) else {}
    raw_batch = hints.get("max_batch_size")
    max_batch_size = (
        raw_batch
        if isinstance(raw_batch, int) and not isinstance(raw_batch, bool) and raw_batch > 0
        else None
    )
    raw_rate = hints.get("sample_rate_override")
    sample_rate_override = (
        float(raw_rate)
        if isinstance(raw_rate, (int, float))
        and not isinstance(raw_rate, bool)
        and 0.0 <= float(raw_rate) <= 1.0
        else None
    )
    raw_wait = hints.get("retry_after_ms")
    retry_after_ms = (
        raw_wait
        if isinstance(raw_wait, int) and not isinstance(raw_wait, bool) and raw_wait >= 0
        else None
    )
    return _BatchAck(
        accepted_spans=accepted_spans,
        errors=tuple(errors),
        max_batch_size=max_batch_size,
        sample_rate_override=sample_rate_override,
        retry_after_ms=retry_after_ms,
    )


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
            # The 2xx body is read, not discarded: a partial batch is a 200 whose refusals are
            # stated only in the body, so dropping it here is what lost the spans in CTO-391. A
            # body we cannot read costs us the per-item detail, never the send.
            try:
                ack = resp.read(_MAX_ACK_BYTES)
            except Exception:  # noqa: BLE001 - an unreadable ack still leaves a valid status
                ack = b""
            return SendResult(int(resp.status), None, ack)
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
        # The ceiling the CALLER chose. ``server_hints.max_batch_size`` is a ceiling the gateway
        # asks for, not a target, so an applied hint may only lower the batch below this; a healthy
        # hint (1000) must never silently enlarge a caller's deliberate 512 (CTO-391).
        self._configured_max_batch_size = max_batch_size

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
        # Spans the gateway refused INSIDE a 200 with a code no resend can fix, plus any shortfall
        # it did not name. Kept apart from rejected_span_count (a whole batch refused by status)
        # because the two have different fixes, and merging them would hide which one happened
        # (CTO-391).
        self.rejected_by_gateway_span_count = 0
        # Spans handed back to the buffer after a RETRYABLE in-200 refusal. Not a loss, so not in
        # shed_counts(): it is the running total of spans that got a second chance, and it is the
        # figure that says "the gateway is shedding" while every loss counter stays at zero.
        self.requeued_span_count = 0
        # Consecutive flushes that ended in a re-enqueue. Bounds the resend loop: a gateway that
        # sheds forever would otherwise trade the same spans back and forth forever (CTO-391).
        self._partial_retry_rounds = 0
        #: Flow-control advice from the last readable ack, exposed for callers who want to see it.
        self.last_server_hints: dict[str, object] | None = None
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
            ack: _BatchAck | None = None
            try:
                result = self._sender(self._url, headers, body)
                if isinstance(result, SendResult):
                    status, hint = result.status, result.retry_after_ms
                    ack = _parse_ack(result.body)
                else:
                    status = int(result)
                ok = 200 <= status < 300
                retryable = ok or self._is_retryable(status)
            except Exception as exc:  # noqa: BLE001 - transport errors must never escape
                self.obs.record_error(exc, "BatchingTransport.flush")

            with self._lock:
                if ok:
                    self._pending = None
                    # A 200 says the batch was answered, not that every span landed: the refusals
                    # live in the body (CTO-391). The batch is still unpinned here, so a span that
                    # has to go again is re-enqueued by the accounting below rather than left in a
                    # pinned batch that a later resend would ship in full, duplicating the spans
                    # the gateway already accepted.
                    self._account_ack_locked(batch, ack)
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
        first, rolled_events, rolled_spans = self._damp_locked(cause, lost)
        if first:
            _log.warning(
                "tally: shed %d span(s), batch %s (further sheds from this cause are summarized "
                "every %d; see shed_counts())",
                lost,
                why,
                _SHED_LOG_EVERY,
            )
        elif rolled_events:
            _log.warning(
                "tally: shed %d more batch(es), %d span(s), still %s",
                rolled_events,
                rolled_spans,
                cause,
            )

    def _damp_locked(self, cause: str, spans: int) -> tuple[bool, int, int]:
        """Decide whether ``cause`` may speak in the log now. Caller holds _lock.

        Returns ``(is_first, rolled_events, rolled_spans)``: the first occurrence of a cause speaks
        in full, the rest accumulate silently and surface as one rolled-up line per
        ``_SHED_LOG_EVERY``. Factored out of :meth:`_shed_locked` so the in-200 rejection warning
        damps on exactly the same terms: a gateway shedding under load rejects items on every flush
        for as long as the load lasts, so an undamped line there would flood the log just as a
        standing 401 did (CTO-391).
        """
        seen = self._shed_log_state.get(cause)
        if seen is None:
            self._shed_log_state[cause] = [0, 0]
            return True, 0, 0
        seen[0] += 1
        seen[1] += spans
        if seen[0] >= _SHED_LOG_EVERY:
            rolled = (False, seen[0], seen[1])
            seen[0] = 0
            seen[1] = 0
            return rolled
        return False, 0, 0

    # --- CTO-391: per-item outcomes reported inside a 200 ---
    def _account_ack_locked(self, batch: BatchRequest, ack: _BatchAck | None) -> None:
        """Account for what the gateway actually accepted out of a delivered batch.

        The gateway answers 200 / ``status: partial`` when only some items were refused, so the
        status alone cannot tell a full success from a batch that lost billable spend. Retryable
        refusals go back on the buffer, permanent ones are counted and warned about, flags are
        neither. Caller holds _lock.
        """
        self._apply_hints_locked(ack)
        sent = batch.resource_spans
        if ack is None or ack.accepted_spans is None:
            # An ack we could not read says nothing about what landed. Inventing a loss here would
            # be as dishonest as the silent success this fixes, so the batch clears on the status
            # alone, exactly as it did before (CLAUDE.md, honest under uncertainty).
            self._consecutive_failures = 0
            self._retry_after_ms = None
            self._partial_retry_rounds = 0
            return

        retry_positions, dead_positions, codes = self._classify_rejections(sent, ack)
        # Spans the gateway neither accepted nor named. They are gone and we cannot tell why, so
        # they are counted with the permanent losses rather than resent: a resend of a span the
        # gateway may in fact have written would double-count spend, which nothing downstream can
        # undo (#311).
        named = len(retry_positions) + len(dead_positions)
        unattributed = max(0, len(sent) - ack.accepted_spans - named)
        lost = len(dead_positions) + unattributed
        if lost:
            self.rejected_by_gateway_span_count += lost
            self.obs.dropped_span_count += lost

        requeued = 0
        if retry_positions:
            if self._partial_retry_rounds < self.retry_max:
                self._partial_retry_rounds += 1
                requeued = self._requeue_locked([sent[i] for i in retry_positions])
            else:
                # Budget spent. A gateway shedding without end is an ingest availability problem,
                # which is what undelivered_span_count already means, and a bounded loop is the
                # whole reason the count exists rather than an endless resend.
                lost += len(retry_positions)
                self.undelivered_span_count += len(retry_positions)
                self.obs.dropped_span_count += len(retry_positions)
                self._partial_retry_rounds = 0

        if lost or requeued:
            # Codes and counts only: an item_id is a trace/span id and a span is the customer's
            # data, so neither goes near the log (CLAUDE.md, no bodies in telemetry).
            summary = ", ".join(f"{code}={n}" for code, n in sorted(codes.items()))
            first, rolled_events, rolled_spans = self._damp_locked("gateway rejected items", lost)
            if first:
                _log.warning(
                    "tally: gateway accepted %d of %d span(s) inside a 200 (%s); %d re-enqueued, "
                    "%d lost (further occurrences are summarized every %d; see shed_counts())",
                    ack.accepted_spans,
                    len(sent),
                    summary or "no codes named",
                    requeued,
                    lost,
                    _SHED_LOG_EVERY,
                )
            elif rolled_events:
                _log.warning(
                    "tally: %d more partial ack(s), %d span(s) lost, gateway still rejecting items",
                    rolled_events,
                    rolled_spans,
                )

        if requeued:
            # Treated as a failed flush for pacing: the gateway just told us it is shedding, so the
            # resend waits out its hint (or our backoff) instead of arriving immediately.
            self._consecutive_failures += 1
            self._retry_after_ms = ack.retry_after_ms
        else:
            self._consecutive_failures = 0
            self._retry_after_ms = None
            self._partial_retry_rounds = 0

    def _classify_rejections(
        self, sent: list[dict[str, object]], ack: _BatchAck
    ) -> tuple[list[int], list[int], dict[str, int]]:
        """Split the ack's refusals into retryable and permanent positions in ``sent``.

        A code we cannot place (an item_id that is not ours) is counted in ``codes`` for the log but
        not resent: guessing which span it meant could resend one the gateway accepted.
        """
        positions = self._item_positions(sent)
        retry: dict[int, None] = {}
        dead: dict[int, None] = {}
        codes: dict[str, int] = {}
        for item_id, code in ack.errors:
            if code in _FLAG_ITEM_CODES:
                continue  # accepted-but-flagged: advice about the span, not a loss of it
            codes[code] = codes.get(code, 0) + 1
            pos = positions.get(item_id)
            if pos is None:
                continue
            if code in _NON_RETRYABLE_ITEM_CODES:
                retry.pop(pos, None)  # one permanent verdict settles the item
                dead[pos] = None
            elif pos not in dead:
                retry[pos] = None
        return list(retry), list(dead), codes

    @staticmethod
    def _item_positions(sent: list[dict[str, object]]) -> dict[str, int]:
        """Map the gateway's ``item_id`` spelling back to positions in the batch we sent.

        Mirrors ``gateway/validation.py`` ``span_item_id`` over ``BatchRequest.deduplicated()``
        (wire.py): the gateway numbers items AFTER intra-batch dedup, so numbering the raw list
        would hand back the wrong span for any batch that carried a duplicate.

        One deliberate divergence: a span with no trace/span id is numbered, never skipped. The
        gateway's dedup keys such a span as ``(None, None)``, so a strict mirror would treat every
        id-less span after the first as a duplicate and refuse to place any of them, which is
        exactly the population the SDK emits (spans carry gen_ai.* attributes, not ids). Being
        tolerant here can only ever place an item the gateway itself named; anything it did not name
        still falls into the unattributed shortfall and is counted rather than resent, so this
        cannot resend a span that was accepted (CTO-391).
        """
        positions: dict[str, int] = {}
        seen: set[tuple[object, object]] = set()
        index = 0
        for pos, span in enumerate(sent):
            if isinstance(span, dict):
                trace = span.get("TraceId") or span.get("trace_id")
                span_id = span.get("SpanId") or span.get("span_id")
                if trace and span_id:
                    if (trace, span_id) in seen:
                        continue  # the gateway dropped this one before numbering
                    seen.add((trace, span_id))
                    item_id = f"{trace}:{span_id}"
                else:
                    item_id = f"#{index}"
            else:
                item_id = f"#{index}"
            positions.setdefault(item_id, pos)
            index += 1
        return positions

    def _requeue_locked(self, spans: list[dict[str, object]]) -> int:
        """Put refused-but-retryable spans back at the FRONT of the buffer. Caller holds _lock.

        Front, because they were metered before everything still queued and ordering keeps the next
        batch contiguous. The buffer cap still wins: a re-enqueue that would exceed it drops oldest
        and counts, exactly as export() does, so a shedding gateway can never grow this buffer past
        the bound the caller set.
        """
        self._buf.extendleft(reversed(spans))
        while len(self._buf) > self.max_buffer:
            self._buf.popleft()
            self.obs.dropped_span_count += 1
            self.buffer_overflow_span_count += 1
        self.requeued_span_count += len(spans)
        return len(spans)

    def _apply_hints_locked(self, ack: _BatchAck | None) -> None:
        """Honour the flow-control advice a 200 carries. Caller holds _lock.

        ``max_batch_size`` is applied as a ceiling only (never above what the caller configured), so
        an overloaded gateway asking for 250 is obeyed and a healthy one asking for 1000 cannot
        enlarge a deliberate 512. ``sample_rate_override`` is recorded but deliberately NOT applied:
        acting on it means dropping spans that were already metered, which is a head-sampling
        decision that belongs with the sampler and the billing-at-head rule, not in the transport
        (CTO-391; see the PR for the reasoning).
        """
        if ack is None:
            return
        self.last_server_hints = {
            "max_batch_size": ack.max_batch_size,
            "sample_rate_override": ack.sample_rate_override,
            "retry_after_ms": ack.retry_after_ms,
        }
        if ack.max_batch_size is not None:
            self.max_batch_size = max(1, min(self._configured_max_batch_size, ack.max_batch_size))

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
                "rejected_by_gateway_span_count": self.rejected_by_gateway_span_count,
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
                self.flush_once()
                # Paced by the backoff whenever one is owed, which a clean delivery clears to zero.
                # Reading it instead of branching on the return value is what makes a PARTIAL ack
                # pace correctly: it delivered, so flush_once returns True, but the gateway just
                # said it is shedding and the re-enqueued spans must not go straight back at it
                # (CTO-391).
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
