"""Ingest burst buffer — pure logic with an in-memory mock backend (CTO-37).

In production a Kafka topic absorbs ingest bursts so the gateway never returns 5xx under load and a
slow ClickHouse can't backpressure the edge. This module is the *transport-agnostic* core: a
:class:`BufferRecord`, a producer that assigns partitions (:mod:`gateway.partition`), a
:class:`InMemoryBuffer` mock that preserves per-partition FIFO order, and a :class:`BufferConsumer`
that drains to a store with **at-least-once + dedup** (pairs with CTO-32 idempotency).

The real Kafka producer/consumer is a later infra ticket; this lets us build and test partition
ordering, fairness, and at-least-once dedup now without any broker running.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from gateway.partition import DEFAULT_PARTITIONS, trace_partition


@dataclass(frozen=True, slots=True)
class BufferRecord:
    """One span en route to ClickHouse, tagged with its routing identity."""

    tenant_id: str
    trace_id: str
    span_id: str
    partition: int
    row: tuple[object, ...]

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.trace_id, self.span_id)


class SpanStore(Protocol):
    def insert_spans(self, rows: list[tuple[object, ...]]) -> int: ...


def to_records(
    tenant_id: str,
    spans: Iterable[tuple[str, str, tuple[object, ...]]],
    partitions: int = DEFAULT_PARTITIONS,
) -> list[BufferRecord]:
    """Build partition-tagged records from ``(trace_id, span_id, row)`` triples."""
    out: list[BufferRecord] = []
    for trace_id, span_id, row in spans:
        out.append(
            BufferRecord(
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=span_id,
                partition=trace_partition(tenant_id, trace_id, partitions),
                row=row,
            )
        )
    return out


@dataclass(slots=True)
class InMemoryBuffer:
    """A mock burst buffer: one FIFO queue per partition (stands in for a Kafka topic).

    Per-partition order is preserved on drain, so spans of a trace consume in produce order. Drains
    are *fair across tenants* — a round-robin over tenants prevents one tenant's burst from starving
    others sharing a partition.
    """

    partitions: int = DEFAULT_PARTITIONS
    _queues: dict[int, deque[BufferRecord]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.partitions < 1:
            raise ValueError("partitions must be >= 1")
        self._queues = {p: deque() for p in range(self.partitions)}

    def produce(self, records: Iterable[BufferRecord]) -> int:
        n = 0
        for rec in records:
            self._queues[rec.partition].append(rec)
            n += 1
        return n

    def depth(self) -> int:
        """Total buffered records across all partitions."""
        return sum(len(q) for q in self._queues.values())

    def drain(self, max_records: int) -> list[BufferRecord]:
        """Pop up to ``max_records`` fairly across tenants, preserving per-partition order.

        Within a partition we always take from the front (FIFO). Across the buffer we round-robin by
        tenant so no single tenant monopolizes a drain. Returns fewer than ``max_records`` only when
        the buffer empties.
        """
        if max_records <= 0:
            return []
        out: list[BufferRecord] = []
        # Snapshot the per-tenant head order so the round-robin is deterministic.
        while len(out) < max_records:
            took_any = False
            seen_tenants: set[str] = set()
            for p in range(self.partitions):
                q = self._queues[p]
                if not q:
                    continue
                tenant = q[0].tenant_id
                if tenant in seen_tenants:
                    continue  # already served this tenant this round → fairness
                seen_tenants.add(tenant)
                out.append(q.popleft())
                took_any = True
                if len(out) >= max_records:
                    break
            if not took_any:
                break
        return out


class BufferConsumer:
    """Drains buffered records to a store with at-least-once delivery + dedup.

    Dedup is on ``(tenant_id, trace_id, span_id)`` so a redelivered record (Kafka at-least-once, or a
    replayed batch) is written exactly once. On a store failure nothing is marked seen and the count
    is 0 — the records are still in the buffer, so the next drain retries them (at-least-once).
    """

    def __init__(self, store: SpanStore) -> None:
        self._store = store
        self._seen: set[tuple[str, str, str]] = set()

    def consume(self, records: list[BufferRecord]) -> int:
        """Write the not-yet-seen rows; return how many were newly written (0 on store failure)."""
        fresh = [r for r in records if r.dedup_key not in self._seen]
        if not fresh:
            return 0
        rows = [r.row for r in fresh]
        written = self._store.insert_spans(rows)  # may raise → caller's records stay un-acked
        for r in fresh:
            self._seen.add(r.dedup_key)
        return written
