"""Async ingest burst buffer that decouples ClickHouse from the request edge (CTO-37).

:mod:`gateway.buffer` is the transport-agnostic core (partition-tagged records, a FIFO/fair
in-memory queue, an at-least-once + dedup consumer). This module is the *service* that wires that
core into the running gateway: a non-blocking :meth:`produce_rows` the request path calls, plus a
background drain loop that writes to ClickHouse off the hot path.

Why this exists (spec §4.5, §11): a burst of ingest must never return 5xx, and a slow or briefly
unavailable ClickHouse must not backpressure the edge. By accepting span rows into an in-memory
buffer and acking the client immediately, the gateway's tail latency is decoupled from ClickHouse's
write latency. The buffer drains fairly across tenants (so one tenant's burst can't starve others)
and re-enqueues a drained chunk if the store write fails (at-least-once; dedup makes redelivery
safe).

In production the in-memory queue is replaced by a Kafka topic partitioned by
``(tenant_id, trace_id_hash % N)`` — the same partition function (:func:`gateway.partition`) and the
same :class:`~gateway.buffer.BufferConsumer` semantics carry over unchanged; only the queue's backing
store differs. Keeping the mock here lets the gateway run, and the fairness/at-least-once/burst
behavior be tested, without a broker.

The drain loop runs the (synchronous) ClickHouse insert via :func:`asyncio.to_thread` so it never
blocks the event loop, and all buffer mutations are guarded by a lock because produce (event-loop
thread) and drain (worker thread) touch the same queues.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from gateway.buffer import BufferConsumer, InMemoryBuffer, SpanStore, to_records
from gateway.partition import DEFAULT_PARTITIONS

logger = logging.getLogger("tally.gateway.buffer")

# Row-tuple indices for the routing identity (must match gateway.mapping.COLUMNS).
_TRACE_ID_COL = 2
_SPAN_ID_COL = 3


@dataclass(frozen=True, slots=True)
class ProduceResult:
    """Outcome of buffering one batch's rows."""

    accepted: int  # rows enqueued for durable async write
    rejected: int  # rows shed because the buffer was at capacity (client should retry these)
    depth: int  # total buffered rows after this produce


class AsyncIngestBuffer:
    """In-memory burst buffer with a background drain to a :class:`~gateway.buffer.SpanStore`.

    ``capacity`` is a high-water mark: producing never blocks, but rows beyond the cap are *shed*
    (reported as ``rejected`` so the request path can return them as retryable, never a 5xx). With a
    generous cap the buffer only saturates if ClickHouse is down long enough to fill it — exactly the
    case where shedding the overflow as retryable is the correct backpressure signal.
    """

    def __init__(
        self,
        store: SpanStore,
        *,
        partitions: int = DEFAULT_PARTITIONS,
        capacity: int = 200_000,
        drain_batch: int = 2_000,
        poll_interval_s: float = 0.05,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if drain_batch < 1:
            raise ValueError("drain_batch must be >= 1")
        self._buffer = InMemoryBuffer(partitions)
        self._consumer = BufferConsumer(store)
        self._partitions = partitions
        self._capacity = capacity
        self._drain_batch = drain_batch
        self._poll = poll_interval_s
        self._lock = threading.Lock()  # guards buffer queue mutations
        self._consume_lock = threading.Lock()  # serializes store writes + dedup if drained concurrently
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._dropped = 0

    @property
    def depth(self) -> int:
        return self._buffer.depth()

    @property
    def dropped(self) -> int:
        """Cumulative rows shed because the buffer was at capacity."""
        return self._dropped

    def produce_rows(self, tenant_id: str, rows: Sequence[tuple[object, ...]]) -> ProduceResult:
        """Enqueue span rows for async write. Non-blocking; sheds overflow past ``capacity``.

        ``rows`` are full ``otel_spans`` tuples (see :func:`gateway.mapping.span_to_row`); the routing
        identity is read from the TraceId/SpanId columns so the buffer can partition and dedup without
        re-deriving anything.
        """
        with self._lock:
            free = max(0, self._capacity - self._buffer.depth())
            accept = list(rows[:free])
            rejected = len(rows) - len(accept)
            if accept:
                triples = [(str(r[_TRACE_ID_COL]), str(r[_SPAN_ID_COL]), r) for r in accept]
                self._buffer.produce(to_records(tenant_id, triples, self._partitions))
            self._dropped += rejected
            depth = self._buffer.depth()
        return ProduceResult(accepted=len(accept), rejected=rejected, depth=depth)

    def drain_once(self) -> int:
        """Drain one fair chunk to the store; return rows newly written.

        On a store failure the drained chunk is re-enqueued (at-least-once) and the error re-raised so
        the caller can decide to back off; the consumer's dedup makes the eventual redelivery safe.
        The buffer lock is held only around the queue pops/pushes, never across the (slow) store call,
        so producing stays non-blocking while a write is in flight.
        """
        with self._lock:
            drained = self._buffer.drain(self._drain_batch)
        if not drained:
            return 0
        # Serialize the store write + dedup so a test-thread drain and the background loop can't
        # interleave (double-write / corrupt the dedup set) if both call drain_once concurrently.
        with self._consume_lock:
            try:
                return self._consumer.consume(drained)
            except Exception:
                with self._lock:
                    self._buffer.produce(drained)  # at-least-once: put it back for the next attempt
                raise

    async def start(self) -> None:
        """Launch the background drain loop (idempotent)."""
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="ingest-buffer-drain")

    async def stop(self) -> None:
        """Signal the loop to stop, wait for it, and flush whatever remains (best-effort)."""
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                wrote = await asyncio.to_thread(self.drain_once)
            except Exception:  # noqa: BLE001 - keep the loop alive; rows were re-enqueued
                logger.exception("ingest buffer drain failed; retrying")
                wrote = 0
            if wrote == 0:
                # Nothing to do (or a failed write): sleep briefly, but wake immediately on stop.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
        await asyncio.to_thread(self._flush)

    def _flush(self) -> None:
        """Drain everything remaining on shutdown so a graceful stop doesn't drop buffered rows."""
        while self._buffer.depth():
            try:
                if self.drain_once() == 0:
                    break
            except Exception:  # noqa: BLE001 - store still unhappy at shutdown; give up after logging
                logger.exception("ingest buffer flush failed; %d rows undrained", self._buffer.depth())
                break
