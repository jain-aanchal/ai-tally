"""Pure tests for the buffer partition strategy (CTO-37)."""

from __future__ import annotations

import pytest

from gateway.partition import DEFAULT_PARTITIONS, partition_key, trace_partition


def test_partition_is_deterministic_across_calls() -> None:
    a = trace_partition("t-acme", "trace-123")
    b = trace_partition("t-acme", "trace-123")
    assert a == b  # stable hash, not Python's salted hash()


def test_partition_in_range() -> None:
    for i in range(200):
        p = trace_partition("t-acme", f"trace-{i}")
        assert 0 <= p < DEFAULT_PARTITIONS


def test_all_spans_of_a_trace_share_a_partition() -> None:
    # Per-trace ordering relies on every span of a trace routing to one partition.
    p = trace_partition("t-acme", "trace-xyz")
    for _span in range(10):
        assert trace_partition("t-acme", "trace-xyz") == p


def test_traces_spread_across_partitions() -> None:
    seen = {trace_partition("t-acme", f"trace-{i}") for i in range(500)}
    assert seen == set(range(DEFAULT_PARTITIONS))  # all 4 partitions exercised


def test_tenant_isolation_in_key() -> None:
    # Same trace_id under different tenants may land differently; the key namespaces by tenant.
    assert partition_key("t-a", "trace-1").startswith("t-a:")
    assert partition_key("t-b", "trace-1").startswith("t-b:")


def test_custom_partition_count() -> None:
    for i in range(100):
        assert 0 <= trace_partition("t", f"x{i}", partitions=8) < 8


def test_zero_partitions_rejected() -> None:
    with pytest.raises(ValueError):
        trace_partition("t", "x", partitions=0)
