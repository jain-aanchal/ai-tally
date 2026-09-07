# SPDX-License-Identifier: Apache-2.0
"""Durable batch idempotency against a REAL Postgres (CTO-245).

Separate from ``test_batch_idempotency.py`` because these assert the one thing a fake cannot: that
the SQL itself settles the concurrent race. The claim is a single ``INSERT ... ON CONFLICT DO
NOTHING`` against the primary key, and "exactly one of N racing workers wins" is a property of
Postgres, not of the Python around it. Testing it against an in-memory dict would prove nothing.

Skipped unless ``TALLY_TEST_POSTGRES_DSN`` points at a database with migration 0032 applied, so a
checkout with no infrastructure still runs the suite green. Bring one up on spare ports and run:

    TALLY_TEST_POSTGRES_DSN=postgresql://tally:tally@localhost:55432/tally \\
      uv run --extra dev pytest -q tests/test_batch_idempotency_pg.py
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from tally.wire import BatchResponse, Status

from gateway.batch_idempotency import (
    IN_FLIGHT_LEASE_S,
    IdempotencyStoreUnavailable,
    PostgresIdempotencyStore,
)
from gateway.config import Settings

DSN = os.environ.get("TALLY_TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TALLY_TEST_POSTGRES_DSN to a database with migration 0032 applied"
)


@pytest.fixture
def store() -> PostgresIdempotencyStore:
    return PostgresIdempotencyStore(Settings(postgres_dsn=DSN))


def _tenant() -> str:
    return f"pgtest-{uuid.uuid4()}"


def test_claim_then_replay_returns_the_recorded_response(store: PostgresIdempotencyStore) -> None:
    tenant = _tenant()
    assert store.claim(tenant, "b1", 3600) is None
    store.record(tenant, "b1", BatchResponse(batch_id="b1", accepted_spans=5))
    replay = store.claim(tenant, "b1", 3600)
    assert replay is not None
    assert replay.accepted_spans == 5
    assert replay.status is Status.ACCEPTED


def test_a_new_batch_id_is_claimable(store: PostgresIdempotencyStore) -> None:
    tenant = _tenant()
    assert store.claim(tenant, "b1", 3600) is None
    assert store.claim(tenant, "b2", 3600) is None


def test_exactly_one_of_eight_racing_claims_wins(store: PostgresIdempotencyStore) -> None:
    """THE CONCURRENCY ASSERTION. Eight workers, one batch_id, one processor.

    The seven losers get RETRY rather than a fabricated success, so none of them writes the spans.
    """
    tenant = _tenant()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(tenant, "hot", 3600), range(8)))
    winners = [r for r in results if r is None]
    assert len(winners) == 1
    assert all(r.status is Status.RETRY for r in results if r is not None)


def test_a_claim_still_in_flight_is_retryable_not_accepted(store: PostgresIdempotencyStore) -> None:
    tenant = _tenant()
    assert store.claim(tenant, "b1", 3600) is None
    held = store.claim(tenant, "b1", 3600)
    assert held is not None
    assert held.status is Status.RETRY  # no outcome recorded yet, so no outcome is claimed


def test_a_record_past_the_ttl_is_reclaimed(store: PostgresIdempotencyStore) -> None:
    """Beyond the idempotency window a replay is indistinguishable from a new batch, by design."""
    tenant = _tenant()
    assert store.claim(tenant, "b1", 3600) is None
    store.record(tenant, "b1", BatchResponse(batch_id="b1", accepted_spans=1))
    assert store.claim(tenant, "b1", 0) is None  # a zero-second window ages it out immediately


def test_an_abandoned_in_flight_claim_is_reclaimed_after_its_lease(
    store: PostgresIdempotencyStore,
) -> None:
    """A worker killed mid-batch must not lock the key for the whole 24h window.

    The lease is aged by hand rather than by sleeping :data:`IN_FLIGHT_LEASE_S` seconds.
    """
    tenant = _tenant()
    assert store.claim(tenant, "b1", 3600) is None
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ingest_batch_idempotency "
            "SET updated_at = now() - make_interval(secs => %s) "
            "WHERE tenant_id = %s AND batch_id = %s",
            (float(IN_FLIGHT_LEASE_S + 60), tenant, "b1"),
        )
        conn.commit()
    assert store.claim(tenant, "b1", 3600) is None  # reclaimable, so a retry can make progress


def test_an_unreachable_store_raises_rather_than_returning_none(monkeypatch) -> None:
    """None would mean "process this batch", which on a failed check is how spend gets doubled."""
    broken = PostgresIdempotencyStore(
        Settings(postgres_dsn="postgresql://nobody@127.0.0.1:1/nowhere")
    )
    with pytest.raises(IdempotencyStoreUnavailable):
        broken.claim("t1", "b1", 3600)


def test_prune_removes_only_aged_out_receipts(store: PostgresIdempotencyStore) -> None:
    tenant = _tenant()
    store.claim(tenant, "keep", 3600)
    store.prune(3600)
    assert store.claim(tenant, "keep", 3600) is not None  # still inside the window
    store.prune(0)
    assert store.claim(tenant, "keep", 3600) is None  # pruned, so claimable again
