# SPDX-License-Identifier: Apache-2.0
"""An id-less span gets a deterministic identity, not a fresh random one (CTO-402).

``span_to_row`` used to stamp a fresh random TraceId/SpanId on every id-less span at row-build
time, so two writes of the same logical span got different sorting-key values and the
ReplacingMergeTree backstop (db/clickhouse/otel_spans.sql, ORDER BY (..., Timestamp, TraceId,
SpanId)) could never collapse them. Since CTO-396 correctly stopped ``deduplicated()`` collapsing
id-less spans, an id-less producer retrying a batch under a NEW batch id wrote 500 + 500 rows where
it used to write 1 + 1, and the rollup materialized views fire on INSERT, so that duplicate is
permanent in the rollups.

The retry assertion here is on the SORTING KEY rather than on a post-merge row count: these tests
run against a fake store with no ClickHouse, so a merge cannot be forced. Identical sorting keys
are precisely what the engine collapses on, so that is the property worth pinning.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI
from tally.wire import BatchRequest, encode_request

from gateway.app import app
from gateway.mapping import COLUMNS, span_to_row

_TRACE_COL = COLUMNS.index("TraceId")
_SPAN_COL = COLUMNS.index("SpanId")

# The ReplacingMergeTree sorting key: rows agreeing on all of these are what a merge collapses.
_SORTING_KEY_COLS = (
    "TenantId",
    "FeatureTag",
    "ServiceName",
    "SpanName",
    "Timestamp",
    "TraceId",
    "SpanId",
)


def _sorting_key(row: tuple[object, ...]) -> tuple[object, ...]:
    d = dict(zip(COLUMNS, row, strict=True))
    return tuple(d[c] for c in _SORTING_KEY_COLS)


class FakeStore:
    def __init__(self) -> None:
        self.spans: list[tuple] = []

    def insert_spans(self, rows: list[tuple]) -> int:
        self.spans.extend(rows)
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@contextmanager
def _client() -> Iterator[tuple[TestClient, FakeStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        store = FakeStore()
        app.state.store = store
        yield client, store


def _idless_span(tokens: int, ts_ns: int) -> dict:
    # An explicit client timestamp, so the skew assessment does not clamp against server receive
    # time and hand the two attempts different Timestamps (otel_spans.sql, limitation 3).
    return {
        "timestamp_ns": ts_ns,
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: tokens,
    }


# --- the derivation itself ------------------------------------------------------------------------


def _ids(span: dict, *, tenant_id: str = "t1", ts_ns: int = 1_700_000_000_000_000_000,
         batch_index: int = 0) -> tuple[object, object]:
    row = span_to_row(
        span, tenant_id=tenant_id, effective_ts_ns=ts_ns, batch_index=batch_index
    )
    return row[_TRACE_COL], row[_SPAN_COL]


def test_same_span_at_same_position_derives_the_same_ids() -> None:
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    assert _ids(dict(span)) == _ids(dict(span))


def test_position_in_the_batch_separates_identical_spans() -> None:
    """500 identical-but-distinct spans in one batch must not collapse onto each other."""
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    derived = {_ids(dict(span), batch_index=i) for i in range(500)}
    assert len(derived) == 500


def test_two_tenants_never_share_a_derived_id() -> None:
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    assert _ids(dict(span), tenant_id="t-a") != _ids(dict(span), tenant_id="t-b")


def test_two_moments_never_share_a_derived_id() -> None:
    """Distinct periods included: a derived id must not repeat across billing months."""
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    may = 1_777_000_000_000_000_000
    june = 1_780_000_000_000_000_000
    assert _ids(dict(span), ts_ns=may) != _ids(dict(span), ts_ns=june)


def test_genuinely_different_spans_never_collide() -> None:
    derived = {
        _ids({GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: n}) for n in range(1000)
    }
    assert len(derived) == 1000


def test_trace_and_span_ids_are_independent_and_well_shaped() -> None:
    trace_id, span_id = _ids({GenAI.SYSTEM: "openai"})
    assert trace_id != span_id  # domain-separated, not one digest truncated twice
    assert len(trace_id) == 32 and len(span_id) == 16  # same widths as the random fallback
    assert all(c in "0123456789abcdef" for c in trace_id + span_id)


def test_a_derived_id_leaks_no_attribute_value() -> None:
    """The id is a digest, so no attribute value can be read back out of it."""
    secretish = "acme-corp-internal-project"
    trace_id, span_id = _ids({GenAI.SYSTEM: "openai", "gen_ai.custom.label": secretish})
    assert secretish not in trace_id + span_id


def test_producer_supplied_ids_are_never_replaced() -> None:
    span = {"trace_id": "tr-real", "span_id": "sp-real", GenAI.SYSTEM: "openai"}
    assert _ids(span) == ("tr-real", "sp-real")


def test_only_the_missing_half_is_derived() -> None:
    trace_id, span_id = _ids({"trace_id": "tr-real", GenAI.SYSTEM: "openai"})
    assert trace_id == "tr-real"
    assert len(span_id) == 16


# --- end to end: a retried batch no longer duplicates ---------------------------------------------


def _post(c: TestClient, tenant: str, spans: list[dict]):
    # A fresh BatchRequest each time, so each attempt carries its OWN batch id: exactly the case
    # the durable idempotency store cannot catch, and the case this fix is for.
    batch = BatchRequest(tenant_id=tenant, sdk_version="test", resource_spans=spans)
    return c.post("/v1/batches", json=json.loads(encode_request(batch)))


def test_retried_idless_batch_reproduces_the_same_sorting_keys() -> None:
    ts_ns = time.time_ns()
    spans = [_idless_span(i, ts_ns) for i in range(500)]
    tenant = "t-cto402"

    with _client() as (c, store):
        first = _post(c, tenant, [dict(s) for s in spans])
        assert first.status_code == 200
        assert first.json()["accepted_spans"] == 500
        second = _post(c, tenant, [dict(s) for s in spans])
        assert second.status_code == 200
        assert second.json()["accepted_spans"] == 500

        assert len(store.spans) == 1000  # both attempts wrote; the engine collapses on merge
        first_rows, second_rows = store.spans[:500], store.spans[500:]
        # Every row of the retry has a sorting key identical to its counterpart in the first
        # attempt, which is the identity ReplacingMergeTree collapses on. Before this fix the
        # TraceId/SpanId halves were fresh random values and none of them matched.
        assert [_sorting_key(r) for r in first_rows] == [_sorting_key(r) for r in second_rows]
        # After a merge the 1000 rows are 500 distinct spans.
        assert len({_sorting_key(r) for r in store.spans}) == 500


def test_five_hundred_distinct_idless_spans_still_store_five_hundred_rows() -> None:
    """The CTO-396 property must survive: distinct spans stay distinct, they do not collapse."""
    ts_ns = time.time_ns()
    spans = [_idless_span(i, ts_ns) for i in range(500)]
    with _client() as (c, store):
        r = _post(c, "t-cto402-distinct", spans)
        assert r.status_code == 200
        assert r.json()["accepted_spans"] == 500
        assert len(store.spans) == 500
        assert len({row[_SPAN_COL] for row in store.spans}) == 500
        assert len({_sorting_key(row) for row in store.spans}) == 500
