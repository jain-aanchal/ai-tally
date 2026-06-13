# SPDX-License-Identifier: Apache-2.0
"""Egress — bounded buffer, batch processor, pluggable transport, backoff.

Implements CTO-49.

Telemetry export must never block or crash the host. Spans are appended to a bounded in-memory
buffer (drop-oldest on overflow, counted) and flushed in the background. Transport is pluggable
(a real gRPC OTLP transport sits behind this interface later); failures trigger exponential
backoff with jitter, and the server can push back via :class:`ServerHints`.

Design for testability: the background thread is optional. :meth:`BatchProcessor.flush_once`
performs exactly one flush cycle so tests are deterministic without sleeping.
"""

from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from tally.safety import SelfObservability, safe_block


@dataclass(frozen=True, slots=True)
class ServerHints:
    """Server-driven backpressure. Any field may be None (no change)."""

    suggested_flush_interval_ms: int | None = None
    suggested_max_batch_size: int | None = None
    current_sample_rate_override: float | None = None
    retry_after_ms: int | None = None


class TransportError(Exception):
    """Raised by a transport when a batch cannot be delivered."""


class Transport(Protocol):
    """Pluggable span sink. ``send`` may raise :class:`TransportError`; the processor absorbs it."""

    def send(self, batch: list[dict[str, object]]) -> ServerHints | None: ...


class MemoryTransport:
    """Default no-network transport: records delivered batches. For tests/local dev."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, object]]] = []

    def send(self, batch: list[dict[str, object]]) -> ServerHints | None:
        self.batches.append(list(batch))
        return None

    @property
    def delivered(self) -> list[dict[str, object]]:
        return [span for b in self.batches for span in b]


class FlakyTransport:
    """Test transport that fails the first ``fail_times`` sends, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self._remaining_failures = fail_times
        self.delivered: list[dict[str, object]] = []
        self.attempts = 0

    def send(self, batch: list[dict[str, object]]) -> ServerHints | None:
        self.attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TransportError("simulated outage")
        self.delivered.extend(batch)
        return None


@dataclass(slots=True)
class BackoffPolicy:
    base_ms: int = 100
    max_ms: int = 30_000
    factor: float = 2.0
    jitter: float = 0.25  # +/- fraction
    rng: random.Random = field(default_factory=random.Random)

    def delay_ms(self, consecutive_failures: int) -> float:
        if consecutive_failures <= 0:
            return 0.0
        raw = min(self.base_ms * (self.factor ** (consecutive_failures - 1)), self.max_ms)
        spread = raw * self.jitter
        return max(0.0, raw + self.rng.uniform(-spread, spread))


class BatchProcessor:
    """Bounded buffer + batched, backoff-aware flushing. Enqueue never blocks or raises."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        observability: SelfObservability | None = None,
        max_buffer: int = 10_000,
        max_batch_size: int = 512,
        flush_interval_ms: int = 1_000,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self.obs = observability or SelfObservability()
        self.transport: Transport = transport or MemoryTransport()
        self.max_buffer = max_buffer
        self.max_batch_size = max_batch_size
        self.flush_interval_ms = flush_interval_ms
        self.backoff = backoff or BackoffPolicy()

        self._buf: deque[dict[str, object]] = deque()
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_hints: ServerHints | None = None

    # --- enqueue (hot path) ---
    def enqueue(self, span: dict[str, object]) -> None:
        """Add a span. Never blocks, never raises. Drops oldest on overflow (counted)."""
        with safe_block(self.obs, where="BatchProcessor.enqueue"):
            with self._lock:
                if len(self._buf) >= self.max_buffer:
                    # drop oldest to make room
                    self._buf.popleft()
                    self.obs.dropped_span_count += 1
                self._buf.append(span)

    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    def _take_batch(self) -> list[dict[str, object]]:
        with self._lock:
            n = min(self.max_batch_size, len(self._buf))
            return [self._buf.popleft() for _ in range(n)]

    def _requeue_front(self, batch: list[dict[str, object]]) -> None:
        with self._lock:
            self._buf.extendleft(reversed(batch))
            # enforce cap after requeue (drop oldest)
            while len(self._buf) > self.max_buffer:
                self._buf.popleft()
                self.obs.dropped_span_count += 1

    def _apply_hints(self, hints: ServerHints | None) -> None:
        if hints is None:
            return
        self._last_hints = hints
        if hints.suggested_max_batch_size:
            self.max_batch_size = max(1, hints.suggested_max_batch_size)
        if hints.suggested_flush_interval_ms:
            self.flush_interval_ms = max(0, hints.suggested_flush_interval_ms)

    @property
    def last_hints(self) -> ServerHints | None:
        return self._last_hints

    def flush_once(self) -> bool:
        """Flush a single batch. Returns True on delivery, False on failure/empty.

        On failure the batch is requeued and the failure counter advances (for backoff). Never
        raises into the caller.
        """
        batch = self._take_batch()
        if not batch:
            return False
        try:
            hints = self.transport.send(batch)
        except Exception as exc:  # noqa: BLE001 - boundary; transport errors must never escape
            self._consecutive_failures += 1
            self.obs.record_error(exc, "BatchProcessor.flush")
            self._requeue_front(batch)
            return False
        self._consecutive_failures = 0
        self._apply_hints(hints)
        return True

    def current_backoff_ms(self) -> float:
        return self.backoff.delay_ms(self._consecutive_failures)

    # --- optional background loop ---
    def start(self) -> None:
        if self._thread is not None:
            return

        def _run() -> None:
            while not self._stop.is_set():
                delivered = self.flush_once()
                if delivered:
                    wait = self.flush_interval_ms
                else:
                    backoff = self.current_backoff_ms()
                    wait = backoff if backoff > 0 else self.flush_interval_ms
                self._stop.wait(timeout=max(wait, 1) / 1000.0)
            # drain remaining on stop (best-effort)
            while self.pending() and self.flush_once():
                pass

        self._thread = threading.Thread(target=_run, name="tally-egress", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
