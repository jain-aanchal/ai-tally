# SPDX-License-Identifier: Apache-2.0
"""Durable batch idempotency (CTO-245).

The bug these prove fixed: `(tenant_id, batch_id)` was remembered only inside one gateway process,
so a client retrying a batch across a restart was accepted a second time and its spans written
twice. In a cost product a duplicated span is duplicated money, so "a fresh process must still
recognise the replay" is the load-bearing assertion here, not an edge case.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tally.schema import GenAI
from tally.wire import BatchRequest, BatchResponse, IdempotencyCache, Status

from gateway.app import app
from gateway.batch_idempotency import (
    BatchIdempotency,
    IdempotencyStoreUnavailable,
    _decode_response,
    _encode_response,
)
from gateway.metering import UsageRollup


class _FakeDurable:
    """An in-memory stand-in for Postgres with the same claim/record contract.

    Deliberately NOT a mock of the SQL: the SQL's concurrency guarantee is exercised against a real
    Postgres in ``test_batch_idempotency_pg.py``. This fake models the OUTCOMES the ingest path has
    to handle, so the pipeline's behaviour can be tested without infrastructure.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, BatchResponse | None]] = {}
        self.unavailable = False

    def claim(self, tenant_id: str, batch_id: str, ttl_seconds: float) -> BatchResponse | None:
        if self.unavailable:
            raise IdempotencyStoreUnavailable("fake outage")
        key = (tenant_id, batch_id)
        existing = self.rows.get(key)
        if existing is None:
            self.rows[key] = ("in_flight", None)
            return None
        state, response = existing
        if state == "complete" and response is not None:
            return response
        return BatchResponse(batch_id=batch_id, status=Status.RETRY)

    def record(self, tenant_id: str, batch_id: str, response: BatchResponse) -> None:
        if self.unavailable:
            return  # record is best-effort by design; see the module docstring
        self.rows[(tenant_id, batch_id)] = ("complete", response)


def _gate(durable: _FakeDurable | None) -> BatchIdempotency:
    return BatchIdempotency(IdempotencyCache(ttl_seconds=3600), durable, ttl_seconds=3600)


def _req(batch_id: str, tenant: str = "t1") -> BatchRequest:
    return BatchRequest(tenant_id=tenant, sdk_version="test", batch_id=batch_id)


def test_replay_survives_a_process_restart() -> None:
    """THE BUG. A fresh gate (a restarted gateway) must still recognise a batch it never saw."""
    durable = _FakeDurable()
    first = _gate(durable)
    assert first.check_or_store(_req("b1")) is None  # accepted, gets processed
    first.record(_req("b1"), BatchResponse(batch_id="b1", accepted_spans=3))

    restarted = _gate(durable)  # new process: empty in-process cache, same durable store
    replay = restarted.check_or_store(_req("b1"))
    assert replay is not None
    assert replay.accepted_spans == 3
    assert replay.status is Status.ACCEPTED


def test_a_genuinely_new_batch_is_accepted_after_a_restart() -> None:
    """The fix must not turn dedup into a blanket refusal."""
    durable = _FakeDurable()
    first = _gate(durable)
    assert first.check_or_store(_req("b1")) is None
    first.record(_req("b1"), BatchResponse(batch_id="b1", accepted_spans=3))

    restarted = _gate(durable)
    assert restarted.check_or_store(_req("b2")) is None


def test_same_batch_id_across_tenants_is_two_batches() -> None:
    """The key is (tenant, batch), so a client-generated id colliding across tenants is not a replay."""
    durable = _FakeDurable()
    gate = _gate(durable)
    assert gate.check_or_store(_req("b1", tenant="t1")) is None
    assert gate.check_or_store(_req("b1", tenant="t2")) is None


def test_concurrent_double_submit_is_processed_once() -> None:
    """Second submitter gets RETRY, not a fabricated ACCEPTED and not a second write."""
    durable = _FakeDurable()
    a, b = _gate(durable), _gate(durable)
    assert a.check_or_store(_req("b1")) is None  # A holds the claim
    held = b.check_or_store(_req("b1"))
    assert held is not None
    assert held.status is Status.RETRY  # B must not process it


def test_store_unavailable_raises_rather_than_accepting() -> None:
    """The gate never answers "new batch" from a failed check. Refusing is the honest outcome."""
    durable = _FakeDurable()
    durable.unavailable = True
    with pytest.raises(IdempotencyStoreUnavailable):
        _gate(durable).check_or_store(_req("b1"))


def test_in_process_cache_answers_a_same_process_replay_without_the_store() -> None:
    """The fast path is a hit-only shortcut; a miss must still consult the durable store."""
    durable = _FakeDurable()
    gate = _gate(durable)
    assert gate.check_or_store(_req("b1")) is None
    gate.record(_req("b1"), BatchResponse(batch_id="b1", accepted_spans=7))
    durable.unavailable = True  # a cache hit must not need Postgres at all
    assert gate.check_or_store(_req("b1")).accepted_spans == 7


def test_without_a_durable_store_the_gate_is_the_old_in_process_cache() -> None:
    gate = _gate(None)
    assert gate.durable_enabled is False
    assert gate.check_or_store(_req("b1")) is None
    assert gate.check_or_store(_req("b1")) is not None


def test_response_round_trips_through_the_stored_receipt() -> None:
    original = BatchResponse(batch_id="b1", status=Status.PARTIAL, accepted_spans=4)
    assert _decode_response("b1", _encode_response(original)).status is Status.PARTIAL
    assert _decode_response("b1", _encode_response(original)).accepted_spans == 4


def test_an_unparseable_receipt_is_retryable_not_accepted() -> None:
    """Honest under uncertainty: a receipt we cannot read is not evidence of success."""
    assert _decode_response("b1", "not json").status is Status.RETRY
    assert _decode_response("b1", {"status": "nonsense"}).status is Status.RETRY


# --- the ingest endpoint's behaviour on top of the gate ------------------------------------------


class _CountingStore:
    """Counts what actually reached storage, which is the number that says whether money doubled."""

    def __init__(self) -> None:
        self.spans = 0

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int:
        self.spans += len(rows)
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list[object]) -> int:
        return len(events)

    def insert_identity_links(self, tenant_id: str, links: list[object]) -> int:
        return len(links)

    def close(self) -> None:
        pass


def _batch_body(batch_id: str) -> dict[str, object]:
    return {
        "tenant_id": "t1",
        "sdk_version": "test",
        "batch_id": batch_id,
        "resource_spans": [
            {
                "trace_id": "tr-1",
                "span_id": "sp-1",
                GenAI.FEATURE_TAG: "summarise",
                GenAI.SYSTEM: "openai",
                GenAI.REQUEST_MODEL: "gpt-4o-mini",
                GenAI.USAGE_INPUT_TOKENS: 100,
                GenAI.USAGE_OUTPUT_TOKENS: 20,
            }
        ],
    }


def test_ingest_stores_a_replayed_batch_once_across_a_restart() -> None:
    """End to end: two POSTs of one batch_id, a simulated restart between them, one write."""
    durable = _FakeDurable()
    store = _CountingStore()
    with TestClient(app) as client:
        app.state.store = store
        app.state.metering = UsageRollup()
        app.state.idempotency = _gate(durable)
        first = client.post("/v1/batches", json=_batch_body("b1"))
        assert first.status_code == 200
        assert first.json()["status"] == "accepted"

        # The restart: a brand new gate with an empty cache, exactly as a redeployed worker has.
        app.state.idempotency = _gate(durable)
        second = client.post("/v1/batches", json=_batch_body("b1"))

    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert store.spans == 1  # the whole point: the span was written once, not twice


def test_ingest_refuses_when_the_idempotency_store_is_down() -> None:
    """503 + IDEMPOTENCY_UNAVAILABLE, and nothing written. Visibly broken beats quietly wrong."""
    durable = _FakeDurable()
    durable.unavailable = True
    store = _CountingStore()
    with TestClient(app) as client:
        app.state.store = store
        app.state.metering = UsageRollup()
        app.state.idempotency = _gate(durable)
        resp = client.post("/v1/batches", json=_batch_body("b1"))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_UNAVAILABLE"
    assert resp.json()["status"] == "retry"
    assert store.spans == 0


def test_ingest_tells_a_concurrent_double_submit_to_retry() -> None:
    """The batch another worker is holding is answered retryable and is NOT written a second time."""
    durable = _FakeDurable()
    store = _CountingStore()
    with TestClient(app) as client:
        app.state.store = store
        app.state.metering = UsageRollup()
        gate = _gate(durable)
        app.state.idempotency = gate
        durable.claim("t1", "b1", 3600)  # another worker got there first and has not finished
        resp = client.post("/v1/batches", json=_batch_body("b1"))

    assert resp.status_code == 503
    assert resp.json()["status"] == "retry"
    assert store.spans == 0
