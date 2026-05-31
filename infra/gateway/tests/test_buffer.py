"""Pure tests for the ingest burst buffer: ordering, fairness, at-least-once dedup (CTO-37)."""

from __future__ import annotations

from gateway.buffer import BufferConsumer, BufferRecord, InMemoryBuffer, to_records
from gateway.partition import trace_partition


class FakeStore:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.rows: list[tuple] = []
        self.fail_once = fail_once
        self.calls = 0

    def insert_spans(self, rows: list[tuple]) -> int:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("clickhouse down")
        self.rows.extend(rows)
        return len(rows)


def _rec(tenant: str, trace: str, span: str, payload: object) -> BufferRecord:
    return BufferRecord(
        tenant_id=tenant,
        trace_id=trace,
        span_id=span,
        partition=trace_partition(tenant, trace),
        row=(payload,),
    )


def test_to_records_assigns_matching_partition() -> None:
    recs = to_records("t-acme", [("trace-1", "s1", ("row1",)), ("trace-1", "s2", ("row2",))])
    assert len(recs) == 2
    assert recs[0].partition == trace_partition("t-acme", "trace-1")
    assert recs[0].partition == recs[1].partition  # same trace → same partition (ordering)


def test_produce_and_depth() -> None:
    buf = InMemoryBuffer()
    n = buf.produce(_rec("t", f"trace-{i}", f"s{i}", i) for i in range(5))
    assert n == 5
    assert buf.depth() == 5


def test_drain_preserves_per_partition_fifo_order() -> None:
    buf = InMemoryBuffer()
    # All spans of one trace land in one partition → must drain in produce order.
    recs = [_rec("t", "trace-A", f"s{i}", i) for i in range(4)]
    buf.produce(recs)
    drained = buf.drain(10)
    assert [r.span_id for r in drained] == ["s0", "s1", "s2", "s3"]
    assert buf.depth() == 0


def _rec_on(partition: int, tenant: str, trace: str, span: str) -> BufferRecord:
    return BufferRecord(tenant_id=tenant, trace_id=trace, span_id=span, partition=partition, row=(span,))


def test_drain_is_fair_across_tenants() -> None:
    # Fairness is a cross-partition property: the partition hash includes tenant_id, so distinct
    # tenants land on distinct partitions and a round-robin drain serves each once per round.
    buf = InMemoryBuffer(partitions=2)
    buf.produce(_rec_on(0, "hog", f"h-trace-{i}", f"s{i}") for i in range(5))  # floods partition 0
    buf.produce([_rec_on(1, "small", "s-trace", "s0")])  # one record on partition 1
    first_round = buf.drain(2)  # one drain round should serve each tenant once
    tenants = {r.tenant_id for r in first_round}
    assert "small" in tenants  # the small tenant is not starved behind the hog's burst
    assert "hog" in tenants


def test_consumer_at_least_once_dedup() -> None:
    store = FakeStore()
    consumer = BufferConsumer(store)
    recs = [_rec("t", "trace-A", "s1", "x"), _rec("t", "trace-A", "s2", "y")]
    assert consumer.consume(recs) == 2
    # Redelivery of the same records (Kafka at-least-once) must not double-write.
    assert consumer.consume(recs) == 0
    assert len(store.rows) == 2


def test_consumer_retries_after_store_failure() -> None:
    store = FakeStore(fail_once=True)
    consumer = BufferConsumer(store)
    recs = [_rec("t", "trace-A", "s1", "x")]
    # First attempt: store raises → nothing acked, records remain eligible.
    try:
        consumer.consume(recs)
    except RuntimeError:
        pass
    assert store.rows == []
    # Retry succeeds and writes exactly once (at-least-once delivery).
    assert consumer.consume(recs) == 1
    assert len(store.rows) == 1
