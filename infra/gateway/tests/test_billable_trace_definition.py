# SPDX-License-Identifier: Apache-2.0
"""What counts as one billable trace, pinned per ingest path (CTO-403).

``GET /v1/usage`` bills on ``uniqExactIf(TraceId, notEmpty(TraceId))`` over ``otel_spans``
(CTO-390), so the invoice base is whatever ``TraceId`` the write path put on the stored rows.
CTO-396 changed that for SDK traffic without anything failing: five spans inside one ``start_trace``
used to bill as five and now bill as one. Nothing pinned the old shape, so nothing objected.

These tests take spans through the real ingest path with a fake store and count the stored rows the
way the invoice query counts them. They assert the definition written in
``docs/billable-trace-definition.md``, which includes the parts that document calls wrong: a
trace-less span billing as a whole trace is pinned here as CURRENT behaviour, not as endorsed
behaviour. Changing it should change these tests deliberately, with the pricing consequence in hand.

No ClickHouse: :func:`billable_traces` mirrors the production SQL over the rows the store received,
and :func:`test_the_mirror_matches_the_production_query` keeps the mirror honest.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from tally.client import MemoryExporter, TallyClient
from tally.context import start_trace
from tally.pricing import Usage, seed_catalog
from tally.sampling import Sampler, SamplingConfig
from tally.schema import GenAI
from tally.wire import BatchRequest, encode_request

from gateway.app import app
from gateway.mapping import COLUMNS
from gateway.store import ClickHouseStore

_TRACE_COL = COLUMNS.index("TraceId")
TENANT = "t-cto403"


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


def billable_traces(store: FakeStore) -> int:
    """The invoice count over the stored rows: distinct non-empty ``TraceId``.

    The in-test mirror of ``ClickHouseStore.usage_counts``' ``uniqExactIf(TraceId,
    notEmpty(TraceId))``. Exact, not approximate, and an empty id is not a trace.

    It deliberately models only the counting, not the ``TenantId`` and ``Timestamp`` predicates:
    every fixture here posts one tenant's spans in one instant, so those predicates select all rows
    and modelling them would add nothing. They are pinned instead by
    :func:`test_the_mirror_matches_the_production_query`, which asserts the whole statement.
    """
    return len({row[_TRACE_COL] for row in store.spans if row[_TRACE_COL]})


@contextmanager
def _client() -> Iterator[tuple[TestClient, FakeStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        store = FakeStore()
        app.state.store = store
        yield client, store


def _post(c: TestClient, spans: list[dict]):
    body = {"tenant_id": TENANT, "sdk_version": "test", "resource_spans": spans}
    return c.post("/v1/batches", json=body)


def _sdk_spans(n: int, *, traced: bool) -> list[dict]:
    """``n`` spans off the real SDK emit path, inside one trace or with none open."""
    exporter = MemoryExporter()
    client = TallyClient(
        catalog=seed_catalog(),
        exporter=exporter,
        sampler=Sampler(SamplingConfig(body_rate=1.0)),
    )
    if traced:
        with start_trace(feature_tag="checkout_assistant"):
            for _ in range(n):
                client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    else:
        for _ in range(n):
            client.record_llm_call(provider="openai", model="gpt-5-mini", usage=Usage(10, 5))
    return exporter.spans


def _post_sdk(c: TestClient, spans: list[dict]):
    batch = BatchRequest(tenant_id=TENANT, sdk_version="test", resource_spans=spans)
    return c.post("/v1/batches", json=json.loads(encode_request(batch)))


def _proxy_span(i: int) -> dict:
    """One edge-proxy span: its own trace and span id, one request's worth of work.

    Hand-written to match ``infra/edge-proxy/internal/telemetry/telemetry.go``, which stamps a fresh
    ``trace_id`` and ``span_id`` per intercepted request and posts one span per batch. It is NOT the
    Go encoder's output, so what the proxy tests below pin is the DOCUMENT'S CLAIM about the proxy,
    not the proxy itself: if ``telemetry.go`` ever reused a trace id across requests, these tests
    would stay green while ``docs/billable-trace-definition.md``'s proxy row became wrong. Nothing
    in this Python suite can close that gap across the language boundary; the Go side is pinned by
    its own tests.
    """
    return {
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "service_name": "tally-edge-proxy",
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 10,
    }


# --- the SDK paths, which is what CTO-396 moved ------------------------------------------------


def test_five_spans_in_one_start_trace_are_one_billable_trace() -> None:
    """The endorsed unit: one ``start_trace`` scope is one trace, whatever it contains.

    Before CTO-396 this billed five, because every stored row carried the mapper's per-row ``uuid7``
    fallback id. A change that puts it back to five is a five-fold invoice rise for traced SDK
    tenants and needs to be a decision, not a side effect.
    """
    with _client() as (c, store):
        spans = _sdk_spans(5, traced=True)
        assert _post_sdk(c, spans).status_code == 200
        assert len(store.spans) == 5  # CTO-396: every span is stored, not collapsed to one
        assert billable_traces(store) == 1


def test_five_trace_less_spans_are_five_billable_traces() -> None:
    """Current behaviour, and the document does not endorse it (CTO-401).

    A span emitted with no ``start_trace`` open gets its own trace id, so a per-span charge is
    billed under the name of a trace. Pinned because either fix under discussion moves the invoice:
    emitting an empty id drops these spans from the count entirely (``notEmpty``), and marking the
    id synthetic only helps if the invoice query learns about the marking.
    """
    with _client() as (c, store):
        spans = _sdk_spans(5, traced=False)
        assert _post_sdk(c, spans).status_code == 200
        assert len(store.spans) == 5
        assert billable_traces(store) == 5


def test_the_same_five_calls_bill_differently_with_and_without_a_trace() -> None:
    """The gap stated as one assertion: identical work, five times the bill (CTO-403)."""
    with _client() as (c, store):
        _post_sdk(c, _sdk_spans(5, traced=True))
        traced = billable_traces(store)
    with _client() as (c, store):
        _post_sdk(c, _sdk_spans(5, traced=False))
        trace_less = billable_traces(store)
    assert (traced, trace_less) == (1, 5)


def test_two_traces_bill_as_two_however_the_spans_are_batched() -> None:
    """Batching must not change the count: the unit is the trace, not the flush interval."""
    with _client() as (c, store):
        first, second = _sdk_spans(3, traced=True), _sdk_spans(3, traced=True)
        assert _post_sdk(c, first + second).status_code == 200  # one batch
        assert billable_traces(store) == 2
    with _client() as (c, store):
        first, second = _sdk_spans(3, traced=True), _sdk_spans(3, traced=True)
        for span in first + second:  # one span per batch
            assert _post_sdk(c, [span]).status_code == 200
        assert billable_traces(store) == 2


# --- the proxy path, which CTO-396 did not touch -----------------------------------------------


def test_proxy_spans_bill_one_trace_per_request() -> None:
    """The proxy stamps a fresh trace id per intercepted request and sends one span per batch, so
    five requests are five billable traces, which is NOT the SDK's unit for the same workload
    (docs/billable-trace-definition.md). How much real traffic arrives this way is unmeasured."""
    with _client() as (c, store):
        for i in range(1, 6):
            assert _post(c, [_proxy_span(i)]).status_code == 200
        assert len(store.spans) == 5
        assert billable_traces(store) == 5


def test_proxy_spans_in_one_batch_still_bill_per_request() -> None:
    """Batching is a transport detail: ids decide the count, not how many POSTs carried them."""
    with _client() as (c, store):
        assert _post(c, [_proxy_span(i) for i in range(1, 6)]).status_code == 200
        assert billable_traces(store) == 5


# --- the OTLP path -----------------------------------------------------------------------------


def test_otlp_spans_bill_by_the_callers_own_trace_ids() -> None:
    """OTLP ids are copied through untouched, so the customer's tracer defines the unit: four spans
    under one ``traceId`` are one billable trace, and a span under its own id is another."""
    otlp = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": f"sp-{i}",
                                "startTimeUnixNano": "1700000000000000000",
                                "attributes": [
                                    {"key": GenAI.SYSTEM, "value": {"stringValue": "openai"}},
                                    {"key": GenAI.OPERATION_NAME, "value": {"stringValue": "chat"}},
                                ],
                            }
                            for i, trace_id in enumerate(
                                ["tr-a", "tr-a", "tr-a", "tr-a", "tr-b"]
                            )
                        ]
                    }
                ],
            }
        ]
    }
    with _client() as (c, store):
        r = c.post("/v1/otlp/traces", json=otlp, headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 200
        assert len(store.spans) == 5
        assert billable_traces(store) == 2


# --- any other id-less client ------------------------------------------------------------------


def test_a_client_that_sends_no_ids_bills_one_trace_per_span() -> None:
    """``mapping.span_to_row`` gives an id-less span a fresh ``uuid7`` trace id per ROW, so a
    third-party or hand-rolled client bills per span. This is the fallback SDK traffic relied on
    before CTO-396, and it is still the rule for everyone else."""
    span = {
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 10,
    }
    with _client() as (c, store):
        assert _post(c, [dict(span) for _ in range(5)]).status_code == 200
        assert len(store.spans) == 5
        assert billable_traces(store) == 5


# --- the mirror ---------------------------------------------------------------------------------


_EXPECTED_USAGE_SQL = (
    "SELECT uniqExactIf(TraceId, notEmpty(TraceId)), "
    "uniqExactIf(FeatureTag, notEmpty(FeatureTag)) "
    "FROM otel_spans "
    "WHERE TenantId = %(t)s AND Timestamp >= %(s)s AND Timestamp < %(e)s"
)


def test_the_mirror_matches_the_production_query() -> None:
    """These tests count rows the way the invoice counts them, so the mirror must not drift.

    If ``usage_counts`` stops counting distinct non-empty ``TraceId``, every assertion above is
    measuring something the bill no longer uses, and this is the test that says so.

    The whole normalised statement is asserted, not a substring of it. A substring check passes
    through exactly the changes that matter here: swapping the two SELECT columns (every assertion
    above would then be measuring feature tags), reading a different table, loosening the window to
    closed on both ends, dropping the tenant predicate, or the divergence
    ``docs/billable-trace-definition.md`` predicts, an added ``WHERE`` term teaching the invoice
    about the CTO-401 synthetic marker. All of those leave the trace-count expression intact and
    make the mirror wrong, so all of them have to fail here.
    """
    captured: dict[str, object] = {}

    class _Result:
        # Distinguishable values, so which SELECT column becomes the trace count is pinned by the
        # return value and not only by the SQL text.
        result_rows = [(7, 3)]

    class _Client:
        def query(self, sql: str, parameters: dict | None = None) -> _Result:
            captured["sql"] = " ".join(sql.split())
            captured["parameters"] = parameters
            return _Result()

    store = ClickHouseStore.__new__(ClickHouseStore)
    store._client = _Client()  # type: ignore[attr-defined]
    store._settings = None  # type: ignore[attr-defined]
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    traces, features = store.usage_counts(TENANT, period_start=start, period_end=end)

    sql = captured["sql"]
    assert sql == _EXPECTED_USAGE_SQL  # table, both columns, order, and the whole WHERE clause
    # Column position, asserted separately so a swap fails with a message about the swap: the trace
    # count must be SELECT column 0, which is the one usage_counts returns first.
    assert sql.startswith("SELECT uniqExactIf(TraceId, notEmpty(TraceId)), ")
    assert (traces, features) == (7, 3)
    # Tenant scoping and the half-open window, which billable_traces above does not model.
    assert captured["parameters"] == {"t": TENANT, "s": start, "e": end}
