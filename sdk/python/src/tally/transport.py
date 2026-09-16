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
  ``UNKNOWN_FEATURE_TAG`` is an accepted-but-flagged marker, so it counts as neither. Items the
  gateway named under an id that matches no span we sent (its buffer-overflow shed numbers the
  overflow by ITS position) are retryable loss we cannot act on: counted apart again, in
  ``unmapped_retryable_span_count``, never booked as a permanent refusal. Anything that leaves this
  client unsure what landed, an ack past the read cap or one whose own numbers do not add up, fails
  LOUDLY: a cap that quietly clears a batch is the very bug this fixes (CTO-391 review).
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
import math
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

    ``body_truncated`` says the ack was longer than the sender was willing to read, so ``body`` is a
    prefix and the outcomes past the cut are unknowable. It is the difference between an ack that
    taught us nothing and one we KNOW was cut short, which the accounting has to treat differently:
    the first clears the batch, the second counts it (CTO-391 review).
    """

    status: int
    retry_after_ms: int | None = None
    body: bytes = b""
    body_truncated: bool = False


#: Sends one POST. Returns a :class:`SendResult`, or a bare status code (the older shape, still
#: accepted so an injected test sender or a caller's own sender keeps working); raises on a
#: network-level failure. Injectable.
Sender = Callable[[str, dict[str, str], bytes], "int | SendResult"]

#: Bounds how much of an ack body is read. The cap only stops a misconfigured endpoint that streams
#: from making us buffer without limit. Nothing from the body is logged or stored: only counts,
#: codes and the integer hints are used.
#:
#: Sized for the ack that actually matters rather than the common one (CTO-391 review). A shed
#: 512-span batch, the SDK's default size, names every item with a real ``trace:span`` id and the
#: gateway's own message text, which measures about 70 KB: the old 64 KiB cap truncated exactly the
#: ack that says spans were lost, and a truncated body parses to nothing. Raised so the realistic
#: worst case fits whole. A cap can still be hit, so hitting one is now reported as truncation and
#: counted, never silently treated as "the gateway had nothing to say".
_MAX_ACK_BYTES = 1024 * 1024

#: Longest body this client will JSON-parse purely to look for a retry hint. A hint is a handful of
#: bytes, but a hostile or broken endpoint can answer every failing attempt with a body at the full
#: ack cap, and parsing a megabyte per attempt, repeated for every retry, is work the endpoint gets
#: to choose for us. Past this we read no hint and fall back to our own bounded backoff, which is
#: exactly what an absent Retry-After already does, so the degradation is honest (CTO-408).
_MAX_HINT_BYTES = 64 * 1024

#: Longest per-item code this client will keep. The gateway's own codes are short identifiers
#: (``IDEMPOTENCY_UNAVAILABLE``, the longest, is 23 characters), so this is generous for anything
#: real while still bounding what a misconfigured or compromised endpoint can push into a log
#: record or hold in this client's memory (CTO-408).
_MAX_CODE_LEN = 64

#: Appended when a code is changed in ANY way, cut or filtered, so an altered string can never
#: collide with a genuine gateway code. Marking only truncation was not enough: filtering alone
#: turned ``"‮PII_DETECTED"`` into the real ``PII_DETECTED``, which this client classifies as a
#: permanent refusal, so a garbage code became a permanent drop of billable spans where before the
#: hygiene it was merely unknown and therefore retried (CTO-408 review). Itself made of safe
#: characters, and counted INSIDE the cap above.
_CODE_ALTERED_MARK = "_ALTERED"

#: How many distinct codes the partial-ack warning names, and the hard ceiling on the joined
#: summary. Bounding each code bounds nothing in aggregate: 512 distinct 64 character codes, well
#: inside the 1 MiB ack cap and so reachable in normal operation, joined into a single 34,990
#: character WARNING record (CTO-408 review). The codes are ranked by count, so the line still
#: names what actually happened and the tail becomes a "+N more" count.
_MAX_SUMMARY_CODES = 5
_MAX_SUMMARY_LEN = 400

#: Characters a code may contain. The gateway's codes are SCREAMING_SNAKE_CASE; the class is a
#: little wider than that so a plausible future code (a digit, a dotted namespace) survives intact
#: rather than being mangled into something that looks like a different code.
_CODE_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)

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


def _read_capped(read: Callable[[int], bytes]) -> tuple[bytes, bool]:
    """Read an ack under :data:`_MAX_ACK_BYTES`, reporting whether the cap cut it short.

    Reads one byte past the cap so a body that exactly fills it is not mistaken for a truncated one.
    A cap that silently returns a prefix is indistinguishable from a short ack, which is how an
    oversized partial ack became a silent batch clear (CTO-391 review). Never raises.
    """
    try:
        raw = read(_MAX_ACK_BYTES + 1)
    except Exception:  # noqa: BLE001 - an unreadable ack still leaves a valid status
        return b"", False
    if len(raw) > _MAX_ACK_BYTES:
        return raw[:_MAX_ACK_BYTES], True
    return raw, False


def _finite_float(raw: object) -> float | None:
    """Coerce an ack number to a finite float, or ``None``. Never raises.

    ``float()`` on a JSON integer of a few hundred digits raises ``OverflowError``, which used to
    escape ``_parse_ack`` (the conversion sat outside the try that wraps ``json.loads``) and make
    the delivered batch look like a failed send, so it was re-sent identically (CTO-391 review).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _safe_code(raw: str) -> str:
    """Reduce a gateway-supplied item code to something safe to hold and to log. Never raises.

    The ack body is attacker-controlled the moment the endpoint is compromised or simply
    misconfigured, and this client joins item codes into a WARNING. Unbounded and unfiltered, a
    code forges a whole log line: 300 characters carrying an embedded ``\\nWARNING injected``
    produced a 491 character record whose second physical line reads exactly like a real one
    (CTO-408). This is log INTEGRITY, not disclosure. The realistic attacker is an endpoint
    poisoning the customer's own log pipeline; the span content and account ids that would be a
    disclosure problem do not reach the records this function feeds, and this change keeps that
    true. That is a statement about these records, not about the file: the partial-ack warning
    does log a gateway-supplied code verbatim, which is exactly why the code is sanitised here.

    Sanitising HERE, at the parse boundary, rather than at the log call, means nothing hostile
    enters this client's state at all: the same string is also held as a dict key for the whole
    flush, so the bound caps memory as well as log width.

    Any alteration is MARKED, and truncation is decided on the RAW length. Filtering silently was
    a behaviour regression: it could rewrite garbage INTO a real code, and a real code is
    classified. ``"\\u202ePII_DETECTED"`` filtered to ``PII_DETECTED``, a permanent refusal, so
    spans that were retried before this hygiene existed were dropped for good after it. Deciding
    truncation on the filtered length had the same shape: ``"RATE_LIMITED" + "!" * 100`` is 112
    characters of hostile input that strips to exactly ``RATE_LIMITED`` and never trips the cap.
    A marked code matches nothing in either code set, so it falls through to unknown and is
    retried, which is the pre-CTO-408 behaviour for a malformed code (CTO-408 review).
    """
    cleaned = "".join(c for c in raw if c in _CODE_SAFE_CHARS)
    # The RAW length decides truncation, so a long hostile code that strips short is still marked.
    altered = cleaned != raw or len(raw) > _MAX_CODE_LEN
    keep = _MAX_CODE_LEN - len(_CODE_ALTERED_MARK)
    if len(cleaned) > keep:
        cleaned = cleaned[:keep]
    if not cleaned:
        # A code made entirely of characters we refuse is still a real outcome the gateway
        # reported, so it keeps a placeholder rather than vanishing: dropping it would silently
        # lose an item outcome, and a lost outcome is the class of bug CTO-391 exists to prevent.
        cleaned, altered = "UNPRINTABLE_CODE", True
    return cleaned + _CODE_ALTERED_MARK if altered else cleaned


def _code_summary(counts: dict[str, int]) -> str:
    """Render per-code counts for one log line, bounded in AGGREGATE as well as per code.

    Bounding each code bounds nothing here: the line joins every DISTINCT code, and an ack naming
    512 of them (comfortably inside the ack read cap) produced a single 34,990 character WARNING
    record. The top codes by count carry the diagnostic value, so they are named and the rest
    become a count (CTO-408 review).
    """
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{code}={n}" for code, n in ranked[:_MAX_SUMMARY_CODES]]
    remaining = len(ranked) - len(parts)
    if remaining:
        parts.append(f"+{remaining} more")
    summary = ", ".join(parts)
    # Belt and braces: the pieces above are individually bounded, so this cannot trip today, but a
    # widened code cap or summary count should not be able to unbound the line by accident.
    return summary if len(summary) <= _MAX_SUMMARY_LEN else summary[:_MAX_SUMMARY_LEN] + "_CUT"


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
                # Bounded and filtered at the boundary, never raw into state or a log (CTO-408).
                # item_id is deliberately left alone: no log record built here carries it (the
                # sweep test pins that), it is only ever compared against ids we generated, and
                # the ack read cap already bounds it.
                errors.append((item_id, _safe_code(code)))

    hints = parsed.get("server_hints")
    hints = hints if isinstance(hints, dict) else {}
    raw_batch = hints.get("max_batch_size")
    max_batch_size = (
        raw_batch
        if isinstance(raw_batch, int) and not isinstance(raw_batch, bool) and raw_batch > 0
        else None
    )
    rate = _finite_float(hints.get("sample_rate_override"))
    sample_rate_override = rate if rate is not None and 0.0 <= rate <= 1.0 else None
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
    if not body or len(body) > _MAX_HINT_BYTES:
        # A body too large to be worth parsing for a hint teaches us nothing and costs real CPU on
        # every failing attempt, so we skip it and use our own backoff (CTO-408). No behaviour is
        # invented here: "no hint" is a state this function already returns and the caller already
        # handles.
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
            # body we cannot read costs us the per-item detail, never the send. A body past the cap
            # is flagged rather than quietly shortened, so the caller can tell "nothing to say"
            # from "more than we read".
            ack, truncated = _read_capped(resp.read)
            return SendResult(int(resp.status), None, ack, truncated)
    except urllib.error.HTTPError as err:
        try:
            ack, truncated = _read_capped(err.read)
        finally:
            err.close()
        return SendResult(int(err.code), _retry_hint_ms(err.headers, ack), b"", truncated)


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
        # Spans the gateway refused with a RETRYABLE code under an item_id that matches no span we
        # sent, so there is nothing to put back. Its buffer-overflow shed names items
        # "#buffer-overflow-N", numbered by the gateway's own overflow position rather than ours
        # (gateway/app.py), so these can never be placed. Counted apart from the permanent refusals
        # because they are NOT permanent: the spans are shed under load and the fix is ingest
        # capacity, not anything in the caller's code (CTO-391 review).
        self.unmapped_retryable_span_count = 0
        # Spans handed back to the buffer after a RETRYABLE in-200 refusal. Not a loss, so not in
        # shed_counts(): it is the running total of spans that got a second chance, and it is the
        # figure that says "the gateway is shedding" while every loss counter stays at zero.
        self.requeued_span_count = 0
        # Consecutive flushes that ended in a re-enqueue. Bounds the resend loop: a gateway that
        # sheds forever would otherwise trade the same spans back and forth forever (CTO-391).
        #
        # KNOWN LIMIT, stated plainly rather than implied (CTO-391 review): this budget is per
        # TRANSPORT, not per span. It counts consecutive partial flushes, so a span refused for the
        # FIRST time during a shedding episode inherits a budget earlier spans already spent and
        # can be dropped after that single refusal, counted in undelivered_span_count as though its
        # own retries had run out. Making it per span means carrying a round count with every
        # buffered span through the pinned-batch path that guarantees no span is sent twice, which
        # is a larger change than this bug fix and is not attempted here. The bound errs toward
        # dropping rather than resending forever, and it resets on any flush that re-enqueues
        # nothing, so the misattribution is confined to a sustained shedding episode.
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
            ack_truncated = False
            try:
                result = self._sender(self._url, headers, body)
                if isinstance(result, SendResult):
                    status, hint = result.status, result.retry_after_ms
                    ack = _parse_ack(result.body)
                    ack_truncated = result.body_truncated
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
                    self._account_ack_locked(batch, ack, truncated=ack_truncated)
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
    def _account_ack_locked(
        self, batch: BatchRequest, ack: _BatchAck | None, *, truncated: bool = False
    ) -> None:
        """Account for what the gateway actually accepted out of a delivered batch.

        The gateway answers 200 / ``status: partial`` when only some items were refused, so the
        status alone cannot tell a full success from a batch that lost billable spend. Retryable
        refusals go back on the buffer, permanent ones are counted and warned about, flags are
        neither. Caller holds _lock.
        """
        self._apply_hints_locked(ack)
        sent = batch.resource_spans
        if truncated:
            # The ack was longer than we were willing to read, so the refusals past the cut are
            # unknowable and the parse above yielded nothing usable. This is NOT the "we learned
            # nothing" case below: we know the gateway had more to say about this batch than we
            # read. Clearing quietly here is precisely the silent loss CTO-391 exists to fix, and
            # it fires at the SDK's default batch size, so the batch is booked as an unattributed
            # shortfall and said out loud. Not resent: the gateway may well have written some of
            # these spans, and a resend would double-count spend nothing downstream can undo
            # (#311).
            lost = len(sent)
            self.rejected_by_gateway_span_count += lost
            self.obs.dropped_span_count += lost
            first, rolled_events, rolled_spans = self._damp_locked("ack truncated", lost)
            if first:
                _log.warning(
                    "tally: gateway ack exceeded the %d byte read cap, so %d span(s) of this batch "
                    "cannot be attributed and are counted as lost (further occurrences are "
                    "summarized every %d; see shed_counts())",
                    _MAX_ACK_BYTES,
                    lost,
                    _SHED_LOG_EVERY,
                )
            elif rolled_events:
                _log.warning(
                    "tally: %d more oversized ack(s), %d span(s) unattributed and counted as lost",
                    rolled_events,
                    rolled_spans,
                )
            self._consecutive_failures = 0
            self._retry_after_ms = None
            self._partial_retry_rounds = 0
            return

        if ack is None or ack.accepted_spans is None:
            # An ack we could not read says nothing about what landed. Inventing a loss here would
            # be as dishonest as the silent success this fixes, so the batch clears on the status
            # alone, exactly as it did before (CLAUDE.md, honest under uncertainty).
            self._consecutive_failures = 0
            self._retry_after_ms = None
            self._partial_retry_rounds = 0
            return

        retry_positions, dead_positions, codes, unmapped_retryable, unmapped_dead = (
            self._classify_rejections(sent, ack)
        )
        named = len(retry_positions) + len(dead_positions) + unmapped_retryable + unmapped_dead
        if ack.accepted_spans + named > len(sent):
            # An ack claiming more outcomes than the batch had items cannot be reconciled with what
            # we sent, and acting on its named positions would re-enqueue spans the same ack
            # claimed to accept: an "accepted_spans: 4" on a 4-span batch that also names #0
            # RATE_LIMITED used to resend span 0 and double-count its spend. So the no-double-send
            # guarantee is not left resting on the gateway being self-consistent: an ack whose own
            # numbers do not add up is distrusted whole and the batch clears on the status alone,
            # which is the pre-CTO-391 behaviour and cannot duplicate anything (CTO-391 review).
            first, rolled_events, _ = self._damp_locked("inconsistent ack", 0)
            if first:
                _log.warning(
                    "tally: gateway ack claims %d accepted plus %d named outcome(s) for a %d span "
                    "batch; distrusting it and clearing on status alone (further occurrences are "
                    "summarized every %d)",
                    ack.accepted_spans,
                    named,
                    len(sent),
                    _SHED_LOG_EVERY,
                )
            elif rolled_events:
                _log.warning(
                    "tally: %d more self-inconsistent ack(s), each cleared on status alone",
                    rolled_events,
                )
            self._consecutive_failures = 0
            self._retry_after_ms = None
            self._partial_retry_rounds = 0
            return

        # Spans the gateway neither accepted nor named. They are gone and we cannot tell why, so
        # they are counted with the permanent losses rather than resent: a resend of a span the
        # gateway may in fact have written would double-count spend, which nothing downstream can
        # undo (#311).
        unattributed = max(0, len(sent) - ack.accepted_spans - named)
        lost = len(dead_positions) + unmapped_dead + unattributed
        if lost:
            self.rejected_by_gateway_span_count += lost
            self.obs.dropped_span_count += lost
        if unmapped_retryable:
            # Retryable, but named under an id that is none of ours, so there is nothing to put
            # back. Counted in its own bucket and warned about separately: booking these as
            # permanent refusals (which is where the unattributed shortfall put them) tells an
            # operator to go and fix client-side data when the real cause is ingest capacity
            # (CTO-391 review). Still not resent, and no id is guessed at: a guess could resend a
            # span the gateway accepted.
            self.unmapped_retryable_span_count += unmapped_retryable
            self.obs.dropped_span_count += unmapped_retryable

        requeued = discarded = 0
        if retry_positions:
            if self._partial_retry_rounds < self.retry_max:
                self._partial_retry_rounds += 1
                requeued, discarded = self._requeue_locked([sent[i] for i in retry_positions])
            else:
                # Budget spent. A gateway shedding without end is an ingest availability problem,
                # which is what undelivered_span_count already means, and a bounded loop is the
                # whole reason the count exists rather than an endless resend.
                lost += len(retry_positions)
                self.undelivered_span_count += len(retry_positions)
                self.obs.dropped_span_count += len(retry_positions)
                self._partial_retry_rounds = 0

        if lost or requeued or discarded:
            # Codes and counts only: an item_id is a trace/span id and a span is the customer's
            # data, so neither goes near the log (CLAUDE.md, no bodies in telemetry). The codes
            # themselves ARE gateway-supplied text and are logged verbatim, which is why they are
            # sanitised at the parse boundary and summarized under an aggregate bound here
            # (CTO-408). The buffer discards are stated separately because they are the one number
            # this line used to get wrong: it reported "4 re-enqueued, 0 lost" for four spans the
            # buffer cap had just thrown away (CTO-391 review).
            summary = _code_summary(codes)
            first, rolled_events, rolled_spans = self._damp_locked("gateway rejected items", lost)
            if first:
                _log.warning(
                    "tally: gateway accepted %d of %d span(s) inside a 200 (%s); %d re-enqueued, "
                    "%d dropped by the buffer cap, %d lost (further occurrences are summarized "
                    "every %d; see shed_counts())",
                    ack.accepted_spans,
                    len(sent),
                    summary or "no codes named",
                    requeued,
                    discarded,
                    lost,
                    _SHED_LOG_EVERY,
                )
            elif rolled_events:
                _log.warning(
                    "tally: %d more partial ack(s), %d span(s) lost, gateway still rejecting items",
                    rolled_events,
                    rolled_spans,
                )

        if unmapped_retryable:
            # Its own line and its own damping key: this is a retryable shed we could not act on,
            # not a refusal, and the two want different responses from whoever reads the log.
            first, rolled_events, rolled_spans = self._damp_locked(
                "gateway shed items we cannot place", unmapped_retryable
            )
            if first:
                _log.warning(
                    "tally: gateway shed %d span(s) with a retryable code under item id(s) that "
                    "match no span in this batch, so they could not be re-enqueued and are counted "
                    "as retryable loss, not as a permanent refusal (further occurrences are "
                    "summarized every %d; see shed_counts())",
                    unmapped_retryable,
                    _SHED_LOG_EVERY,
                )
            elif rolled_events:
                _log.warning(
                    "tally: %d more ack(s) shedding %d span(s) we could not place",
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
    ) -> tuple[list[int], list[int], dict[str, int], int, int]:
        """Split the ack's refusals into retryable and permanent positions in ``sent``.

        Returns ``(retry_positions, dead_positions, codes, unmapped_retryable,
        unmapped_permanent)``.

        A code we cannot place (an item_id that is not ours) is never resent: guessing which span it
        meant could resend one the gateway accepted. It is still COUNTED, and counted according to
        its own code, because an unplaceable id is not evidence that the loss was permanent: the
        gateway's buffer-overflow shed is retryable and names every item this way (CTO-391 review).
        """
        positions = self._item_positions(sent)
        retry: dict[int, None] = {}
        dead: dict[int, None] = {}
        codes: dict[str, int] = {}
        unmapped_retryable = 0
        unmapped_permanent = 0
        for item_id, code in ack.errors:
            if code in _FLAG_ITEM_CODES:
                continue  # accepted-but-flagged: advice about the span, not a loss of it
            codes[code] = codes.get(code, 0) + 1
            permanent = code in _NON_RETRYABLE_ITEM_CODES
            pos = positions.get(item_id)
            if pos is None:
                if permanent:
                    unmapped_permanent += 1
                else:
                    unmapped_retryable += 1
                continue
            if permanent:
                retry.pop(pos, None)  # one permanent verdict settles the item
                dead[pos] = None
            elif pos not in dead:
                retry[pos] = None
        return list(retry), list(dead), codes, unmapped_retryable, unmapped_permanent

    @staticmethod
    def _item_positions(sent: list[dict[str, object]]) -> dict[str, int]:
        """Map the gateway's ``item_id`` spelling back to positions in the batch we sent.

        Mirrors ``gateway/validation.py`` ``span_item_id`` over ``BatchRequest.deduplicated()``
        (wire.py): the gateway numbers items AFTER intra-batch dedup, so numbering the raw list
        would hand back the wrong span for any batch that carried a duplicate.

        The mirror has to be EXACT, because both rules number the same list and any disagreement
        shifts every ``#N`` after the point where they diverge. This used to skip a span only when
        BOTH ids were truthy, while ``deduplicated()`` (CTO-396) drops a duplicate whenever the SPAN
        id is present, trace id or not. Two spans sharing a span id and carrying no trace id
        therefore left the gateway numbering one item ahead of us, so its ``#N`` named a different
        span than ours and a shed item re-enqueued a span the gateway had ACCEPTED: a double send of
        billable spend, which nothing downstream can undo (#311), plus an uncounted loss of the span
        actually shed. The one case where the no-double-send guarantee really broke (CTO-407).

        A span with NO span id is still numbered rather than skipped, and that is the exact mirror
        too, not a tolerance: ``deduplicated()`` passes every such span through, because a missing
        id is not an identity to dedup on. That is the population the SDK itself emits when a
        caller builds spans by hand (spans carry gen_ai.* attributes, not ids).
        """
        positions: dict[str, int] = {}
        seen: set[tuple[object, str]] = set()
        index = 0
        for pos, span in enumerate(sent):
            if isinstance(span, dict):
                trace = span.get("TraceId") or span.get("trace_id")
                span_id = span.get("SpanId") or span.get("span_id")
                if isinstance(span_id, str) and span_id:
                    # Keyed exactly as wire.deduplicated(): on (trace_id, span_id) whenever the span
                    # id is a real string, so an absent trace id cannot switch dedup off here while
                    # it stays on there (CTO-407).
                    key = (trace, span_id)
                    if key in seen:
                        continue  # the gateway dropped this one before numbering
                    seen.add(key)
                item_id = f"{trace}:{span_id}" if (trace and span_id) else f"#{index}"
            else:
                item_id = f"#{index}"
            positions.setdefault(item_id, pos)
            index += 1
        return positions

    def _requeue_locked(self, spans: list[dict[str, object]]) -> tuple[int, int]:
        """Put refused-but-retryable spans back at the FRONT of the buffer. Caller holds _lock.

        Returns ``(requeued, discarded)``: how many survived the buffer cap, and how many the cap
        threw away on the way in.

        Front, because they were metered before everything still queued and ordering keeps the next
        batch contiguous. The buffer cap still wins: a re-enqueue that would exceed it drops oldest
        and counts, exactly as export() does, so a shedding gateway can never grow this buffer past
        the bound the caller set. Drop-oldest is kept rather than trimming the other end, because
        these spans ARE the oldest and reversing that for one path would quietly change the
        documented backpressure policy for everyone else.

        What changed is the counting (CTO-391 review). These spans sit at the front, so they are the
        very first thing the cap discards, and counting all of them as re-enqueued before the trim
        made the warning claim "4 re-enqueued, 0 lost" about four spans that had just been thrown
        away. Only the survivors are counted as re-enqueued; the discards are counted where every
        other buffer-cap drop is counted, and returned so the log can say so.
        """
        self._buf.extendleft(reversed(spans))
        discarded = 0
        while len(self._buf) > self.max_buffer:
            self._buf.popleft()
            self.obs.dropped_span_count += 1
            self.buffer_overflow_span_count += 1
            discarded += 1
        # The buffer held at most max_buffer before the insert, so every discard came out of this
        # re-enqueue; clamped anyway rather than trusting that arithmetic to stay true.
        discarded = min(discarded, len(spans))
        requeued = len(spans) - discarded
        self.requeued_span_count += requeued
        return requeued, discarded

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
                "unmapped_retryable_span_count": self.unmapped_retryable_span_count,
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
