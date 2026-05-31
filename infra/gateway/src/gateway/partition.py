"""Topic partition strategy for the ingest buffer (CTO-37).

A burst buffer (Kafka in prod) sits between the gateway and ClickHouse async inserts. The topic is
partitioned by ``(tenant_id, trace_id_hash % N)`` so that:

* **Per-trace ordering** is preserved — every span of a given ``(tenant_id, trace_id)`` hashes to the
  same partition, so a consumer sees them in produce order.
* **Per-tenant spread** keeps one trace from hot-spotting; a tenant's traces fan out across the N
  partitions rather than serializing on one.

The hash is :func:`hashlib.blake2b` (not Python's salted ``hash()``) so partitioning is *stable across
processes and restarts* — a replayed batch lands on the same partition as the original.
"""

from __future__ import annotations

import hashlib

#: Default partition count (spec: trace_id_hash % 4).
DEFAULT_PARTITIONS = 4


def trace_partition(tenant_id: str, trace_id: str, partitions: int = DEFAULT_PARTITIONS) -> int:
    """Stable partition index in ``[0, partitions)`` for a ``(tenant_id, trace_id)`` pair."""
    if partitions < 1:
        raise ValueError("partitions must be >= 1")
    digest = hashlib.blake2b(
        f"{tenant_id}\x00{trace_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % partitions


def partition_key(tenant_id: str, trace_id: str, partitions: int = DEFAULT_PARTITIONS) -> str:
    """Human-readable partition key ``"<tenant>:<index>"`` (useful for logs/metrics)."""
    return f"{tenant_id}:{trace_partition(tenant_id, trace_id, partitions)}"
