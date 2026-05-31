"""Backpressure + retry semantics — flow-control advice the gateway returns (CTO-36).

The gateway can't push back via TCP alone: a well-behaved SDK self-throttles only if we *tell*
it to. So every response carries :class:`tally.wire.ServerHints` — a flush cadence, a per-batch
ceiling, an optional sample-rate override, and (on a retryable response) a backoff. A conformant
client treats these as the new ceiling until the next response updates them.

This module is pure and clock-free: :class:`Backpressure` maps a live in-flight count to hints and
a shed decision; :func:`is_retryable` classifies an HTTP status. The HTTP wiring (an in-flight gauge
middleware) lives in app.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from tally.wire import ServerHints

# Retryable HTTP statuses: 429 (slow down) and any 5xx (transient server fault). A 4xx other than
# 429 is a client contract error — retrying replays the same rejection, so it is NOT retryable.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503})


def is_retryable(http_status: int) -> bool:
    """True iff a client should retry this response (429 or any 5xx); 4xx contract errors are not."""
    return http_status in _RETRYABLE_STATUSES or 500 <= http_status <= 599


@dataclass(frozen=True, slots=True)
class Shed:
    """Outcome of evaluating current load against an incoming batch.

    ``hints`` is what to echo back to the client. ``keep`` is how many items to admit this batch
    (the rest are shed → retryable PARTIAL). ``overloaded`` flags that we tightened the hints.
    """

    hints: ServerHints
    keep: int
    overloaded: bool


class Backpressure:
    """Pure controller mapping an in-flight request count to client flow-control hints.

    Below ``soft_limit`` the gateway is healthy and emits steady-state hints. At/above it the
    gateway is saturated: it lengthens the flush interval, shrinks the per-batch ceiling, asks the
    client to sample down, and sheds the overflow of the current batch as retryable.
    """

    def __init__(
        self,
        *,
        soft_limit: int = 64,
        healthy_flush_ms: int = 5000,
        healthy_max_batch: int = 1000,
        overload_flush_ms: int = 15000,
        overload_max_batch: int = 250,
        overload_sample_rate: float = 0.25,
        overload_retry_after_ms: int = 2000,
    ) -> None:
        if soft_limit < 1:
            raise ValueError("soft_limit must be >= 1")
        self.soft_limit = soft_limit
        self._healthy = ServerHints(
            flush_interval_ms=healthy_flush_ms,
            max_batch_size=healthy_max_batch,
            sample_rate_override=None,
            retry_after_ms=0,
        )
        self._overloaded = ServerHints(
            flush_interval_ms=overload_flush_ms,
            max_batch_size=overload_max_batch,
            sample_rate_override=overload_sample_rate,
            retry_after_ms=overload_retry_after_ms,
        )

    def healthy_hints(self) -> ServerHints:
        """Steady-state hints (used for replays and the non-overloaded path)."""
        return self._healthy

    def evaluate(self, in_flight: int, batch_items: int) -> Shed:
        """Decide hints + admission for a batch of ``batch_items`` given ``in_flight`` concurrency."""
        if in_flight < self.soft_limit:
            return Shed(hints=self._healthy, keep=batch_items, overloaded=False)
        keep = min(batch_items, self._overloaded.max_batch_size)
        return Shed(hints=self._overloaded, keep=keep, overloaded=True)
