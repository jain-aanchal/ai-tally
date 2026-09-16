# SPDX-License-Identifier: Apache-2.0
from tally.wire import (
    BatchRequest,
    BatchResponse,
    BusinessEvent,
    IdempotencyCache,
    IdentityLink,
    Status,
    decode_request,
    encode_request,
    uuid7,
)


def test_uuid7_is_version_7_and_ordered():
    import time

    a = uuid7()
    assert a[14] == "7"  # version nibble
    time.sleep(0.002)
    b = uuid7()
    # time-ordered: later id sorts after earlier (first 48 bits are ms timestamp)
    assert b > a


def test_codec_roundtrip():
    req = BatchRequest(
        tenant_id="t1",
        sdk_version="py-0.0.1",
        resource_spans=[{"TraceId": "tr", "SpanId": "sp", "x": 1}],
        business_events=[
            BusinessEvent("ev1", "signup", "u" * 64, occurred_at_ns=123)
        ],
        identity_links=[
            IdentityLink("a", "anonymous_id", "b", "user_id", observed_at_ns=1)
        ],
    )
    back = decode_request(encode_request(req))
    assert back.tenant_id == "t1"
    assert back.batch_id == req.batch_id
    assert back.resource_spans == req.resource_spans
    assert back.business_events[0].event_name == "signup"
    assert back.identity_links[0].identity_a_type == "anonymous_id"


def test_intra_batch_span_dedup():
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        resource_spans=[
            {"TraceId": "tr", "SpanId": "s1"},
            {"TraceId": "tr", "SpanId": "s1"},  # dup
            {"TraceId": "tr", "SpanId": "s2"},
        ],
    )
    dd = req.deduplicated()
    assert len(dd.resource_spans) == 2


def test_spans_without_ids_are_never_treated_as_duplicates():
    """CTO-396: the regression. Id-less spans all keyed to (None, None) and collapsed to one."""
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        resource_spans=[{"gen_ai.system": "openai"} for _ in range(5)],
    )
    assert len(req.deduplicated().resource_spans) == 5


def test_spans_with_a_trace_id_but_no_span_id_are_not_collapsed():
    # A shared trace id is not an identity: without a span id these are five different spans.
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        resource_spans=[{"trace_id": "tr"} for _ in range(5)],
    )
    assert len(req.deduplicated().resource_spans) == 5


def test_empty_string_span_id_is_not_an_identity():
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        resource_spans=[{"trace_id": "tr", "span_id": ""} for _ in range(3)],
    )
    assert len(req.deduplicated().resource_spans) == 3


def test_genuine_duplicate_still_collapses_alongside_id_less_spans():
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        resource_spans=[
            {"trace_id": "tr", "span_id": "s1"},
            {"trace_id": "tr", "span_id": "s1"},  # genuine duplicate
            {"gen_ai.system": "openai"},  # id-less, kept
            {"gen_ai.system": "anthropic"},  # id-less, kept
        ],
    )
    assert len(req.deduplicated().resource_spans) == 3


def test_intra_batch_event_and_link_dedup():
    req = BatchRequest(
        tenant_id="t",
        sdk_version="v",
        business_events=[
            BusinessEvent("e1", "x", "u", 1),
            BusinessEvent("e1", "x", "u", 1),  # dup id
        ],
        identity_links=[
            IdentityLink("a", "t1", "b", "t2", 1, source="sdk"),
            IdentityLink("a", "t1", "b", "t2", 1, source="sdk"),  # dup
            IdentityLink("a", "t1", "b", "t2", 1, source="cdp"),  # different source
        ],
    )
    dd = req.deduplicated()
    assert len(dd.business_events) == 1
    assert len(dd.identity_links) == 2


def test_idempotency_returns_cached_on_replay():
    cache = IdempotencyCache()
    req = BatchRequest(tenant_id="t", sdk_version="v")

    first = cache.check_or_store(req)
    assert first is None  # not seen before
    cache.record(
        req, BatchResponse(batch_id=req.batch_id, status=Status.ACCEPTED, accepted_spans=3)
    )

    replay = cache.check_or_store(req)
    assert replay is not None
    assert replay.accepted_spans == 3  # original response returned, not reprocessed


def test_idempotency_distinct_batches_not_deduped():
    cache = IdempotencyCache()
    r1 = BatchRequest(tenant_id="t", sdk_version="v")
    r2 = BatchRequest(tenant_id="t", sdk_version="v")
    assert cache.check_or_store(r1) is None
    assert cache.check_or_store(r2) is None  # different batch_id


def test_idempotency_ttl_expiry():
    clock = {"t": 1000.0}
    cache = IdempotencyCache(ttl_seconds=10, now=lambda: clock["t"])
    req = BatchRequest(tenant_id="t", sdk_version="v")
    cache.check_or_store(req)
    cache.record(req, BatchResponse(batch_id=req.batch_id))
    clock["t"] += 11  # past TTL
    # entry purged → treated as new
    assert cache.check_or_store(req) is None


def test_same_batch_id_different_tenant_isolated():
    cache = IdempotencyCache()
    bid = uuid7()
    a = BatchRequest(tenant_id="A", sdk_version="v", batch_id=bid)
    b = BatchRequest(tenant_id="B", sdk_version="v", batch_id=bid)
    assert cache.check_or_store(a) is None
    cache.record(a, BatchResponse(batch_id=bid, accepted_spans=1))
    # same batch_id but different tenant must NOT collide
    assert cache.check_or_store(b) is None


def test_release_makes_a_reserved_batch_processable_again():
    """CTO-389. A reservation whose attempt failed must not stand in for an answer."""
    cache = IdempotencyCache()
    req = BatchRequest(tenant_id="t", sdk_version="v")
    assert cache.check_or_store(req) is None  # reserved, outcome not yet known
    cache.release(req)
    # Without the release this returns the provisional entry, and the batch is never processed.
    assert cache.check_or_store(req) is None


def test_release_of_an_unseen_batch_is_a_no_op():
    """Callers release unconditionally on a failure path, including one that never reserved."""
    cache = IdempotencyCache()
    cache.release(BatchRequest(tenant_id="t", sdk_version="v"))


def test_release_leaves_a_recorded_outcome_for_the_same_batch_standing():
    """CTO-389 review. A release must never delete a real receipt, only a reservation.

    An unconditional pop would re-admit a batch that has already been answered, which is exactly the
    duplicate write this cache exists to prevent.
    """
    cache = IdempotencyCache()
    req = BatchRequest(tenant_id="t", sdk_version="v")
    cache.check_or_store(req)
    cache.record(req, BatchResponse(batch_id=req.batch_id, accepted_spans=3))
    cache.release(req)
    replay = cache.check_or_store(req)
    assert replay is not None
    assert replay.accepted_spans == 3


def test_release_does_not_touch_a_recorded_outcome_for_another_batch():
    cache = IdempotencyCache()
    kept = BatchRequest(tenant_id="t", sdk_version="v")
    dropped = BatchRequest(tenant_id="t", sdk_version="v")
    cache.check_or_store(kept)
    cache.record(kept, BatchResponse(batch_id=kept.batch_id, accepted_spans=2))
    cache.check_or_store(dropped)
    cache.release(dropped)
    assert cache.check_or_store(kept).accepted_spans == 2


def test_peek_does_not_reserve_the_key() -> None:
    """CTO-245: the gateway's durable layer needs a READ, not a read-that-claims.

    If peek reserved the slot the way check_or_store does, the durable claim that follows it would
    be racing a reservation this process had already taken, and a genuinely new batch would look
    like a replay to itself.
    """
    cache = IdempotencyCache()
    req = BatchRequest(tenant_id="t1", sdk_version="test", batch_id="b1")
    assert cache.peek("t1", "b1") is None
    assert cache.peek("t1", "b1") is None  # still unclaimed after a peek
    assert cache.check_or_store(req) is None  # so the real claim still succeeds


def test_peek_returns_the_recorded_response() -> None:
    cache = IdempotencyCache()
    req = BatchRequest(tenant_id="t1", sdk_version="test", batch_id="b1")
    cache.check_or_store(req)
    cache.record(req, BatchResponse(batch_id="b1", accepted_spans=4))
    hit = cache.peek("t1", "b1")
    assert hit is not None and hit.accepted_spans == 4
