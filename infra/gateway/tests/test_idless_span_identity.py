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

TWO THINGS THESE TESTS DELIBERATELY PIN, beyond "the fix works":

1. The KNOWN COLLAPSE (limitation 4 in otel_spans.sql). Two genuinely distinct spans that agree on
   tenant, posted content, effective timestamp and batch position derive ONE identity, so a merge
   deletes one of them and its spend forever. That is an unavoidable consequence of deriving an id
   for a span that carries none, and it is pinned below so nobody "fixes" it by accident and nobody
   meets it as a surprise in production.
2. That the id is hashed from the POSTED span, not from the post-enrichment attributes. Hashing the
   enriched dict puts the price catalog version and the server-recomputed cost inside the digest,
   so a catalog reload between an attempt and its retry moves the id and the rows stop collapsing.

A NOTE ON WHAT MAKES AN ASSERTION HERE WORTH ANYTHING. A test that only asserts two derived ids
DIFFER also passes against the old random implementation, so it detects no regression at all. Every
separation test below therefore also asserts REPRODUCIBILITY (the same input derives the same id
twice), which randomness fails, while the inequality half keeps a degenerate constant
implementation from passing.
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


def _ids(
    span: dict,
    *,
    tenant_id: str = "t1",
    ts_ns: int = 1_700_000_000_000_000_000,
    batch_index: int = 0,
    identity_span: dict | None = None,
) -> tuple[object, object]:
    row = span_to_row(
        span,
        tenant_id=tenant_id,
        effective_ts_ns=ts_ns,
        batch_index=batch_index,
        identity_span=identity_span,
    )
    return row[_TRACE_COL], row[_SPAN_COL]


def _assert_separates_and_is_reproducible(
    a: tuple[object, object], a_again: tuple[object, object],
    b: tuple[object, object], b_again: tuple[object, object],
) -> None:
    """Both sides reproduce (randomness fails this) and the two sides differ (a constant fails)."""
    assert a == a_again
    assert b == b_again
    assert a != b


def test_same_span_at_same_position_derives_the_same_ids() -> None:
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    assert _ids(dict(span)) == _ids(dict(span))


def test_position_in_the_batch_separates_identical_spans() -> None:
    """500 identical-but-distinct spans in one batch must not collapse onto each other."""
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    first_pass = [_ids(dict(span), batch_index=i) for i in range(500)]
    second_pass = [_ids(dict(span), batch_index=i) for i in range(500)]
    # Reproducible position by position: a random implementation fails here, not on the count.
    assert first_pass == second_pass
    assert len(set(first_pass)) == 500


def test_two_tenants_never_share_a_derived_id() -> None:
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    _assert_separates_and_is_reproducible(
        _ids(dict(span), tenant_id="t-a"), _ids(dict(span), tenant_id="t-a"),
        _ids(dict(span), tenant_id="t-b"), _ids(dict(span), tenant_id="t-b"),
    )


def test_two_moments_never_share_a_derived_id() -> None:
    """Distinct periods included: a derived id must not repeat across billing months."""
    span = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 10}
    may = 1_777_000_000_000_000_000
    june = 1_780_000_000_000_000_000
    _assert_separates_and_is_reproducible(
        _ids(dict(span), ts_ns=may), _ids(dict(span), ts_ns=may),
        _ids(dict(span), ts_ns=june), _ids(dict(span), ts_ns=june),
    )


def test_genuinely_different_spans_never_collide() -> None:
    def derive() -> list[tuple[object, object]]:
        return [_ids({GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: n}) for n in range(1000)]

    first_pass, second_pass = derive(), derive()
    assert first_pass == second_pass  # reproducible, which randomness is not
    assert len(set(first_pass)) == 1000


def test_trace_and_span_ids_are_independent_and_well_shaped() -> None:
    trace_id, span_id = _ids({GenAI.SYSTEM: "openai"})
    assert (trace_id, span_id) == _ids({GenAI.SYSTEM: "openai"})  # reproducible
    assert trace_id != span_id  # domain-separated, not one digest truncated twice
    # Stronger than inequality: the span id must not be a slice of the trace id, which is what a
    # regression to "hash once and truncate twice" would produce.
    assert span_id not in trace_id
    assert len(trace_id) == 32 and len(span_id) == 16  # same widths as the random fallback
    assert all(c in "0123456789abcdef" for c in trace_id + span_id)


def test_a_derived_id_leaks_no_attribute_value() -> None:
    """The id is a digest, so no attribute value can be read back out of it."""
    secretish = "acme-corp-internal-project"
    trace_id, span_id = _ids({GenAI.SYSTEM: "openai", "gen_ai.custom.label": secretish})
    assert secretish not in trace_id + span_id
    # The attribute must still INFLUENCE the id, or "no leak" would be satisfied by ignoring it.
    other = _ids({GenAI.SYSTEM: "openai", "gen_ai.custom.label": "something-else"})
    assert (trace_id, span_id) != other


def test_producer_supplied_ids_are_never_replaced() -> None:
    span = {"trace_id": "tr-real", "span_id": "sp-real", GenAI.SYSTEM: "openai"}
    assert _ids(span) == ("tr-real", "sp-real")


def test_only_the_missing_half_is_derived() -> None:
    span = {"trace_id": "tr-real", GenAI.SYSTEM: "openai"}
    trace_id, span_id = _ids(dict(span))
    assert trace_id == "tr-real"
    assert len(span_id) == 16
    assert _ids(dict(span))[1] == span_id  # the derived half is deterministic too


def test_derivation_is_pinned_to_known_golden_values() -> None:
    """Golden vector: ANY change to the derivation changes these two strings and fails here.

    The behavioural tests around this one each pin a single property, so a change that preserves
    those properties while altering the algorithm (a different personalisation string, a reordered
    or re-serialised material dict, different digest widths) can slip past all of them. This pins
    the output itself, for one fixed input, so the derivation cannot drift unnoticed.

    A derived id is part of the ReplacingMergeTree sorting key, so changing it is not a refactor:
    rows written before the change and rows written after it stop collapsing onto each other, and
    the duplicate-spend bug CTO-402 fixed comes back for every span already in the table. If you are
    changing the derivation deliberately, update this vector, limitation 4 in
    db/clickhouse/otel_spans.sql, and the :func:`gateway.mapping._derive_span_ids` docstring in the
    same commit, and say in the PR that historical rows will not collapse against new ones.
    """
    trace_id, span_id = _ids(
        {GenAI.SYSTEM: "openai", GenAI.OPERATION_NAME: "chat", GenAI.USAGE_INPUT_TOKENS: 42},
        tenant_id="t-golden",
        ts_ns=1_700_000_000_000_000_000,
        batch_index=3,
    )
    assert trace_id == "7c0e08e58173ce6286cbacc16d76d228"
    assert span_id == "61cdf2a22850fa61"


# --- the known collapse, pinned on purpose (otel_spans.sql limitation 4) --------------------------


def test_known_collapse_identical_spans_at_the_same_instant_and_index() -> None:
    """PINS A KNOWN, DOCUMENTED DATA-LOSS CASE. Do not "fix" this test by changing the derivation.

    Two GENUINELY DISTINCT spans that agree on tenant, posted content, effective timestamp and
    batch position are indistinguishable to this derivation, so they get one identity and a
    ReplacingMergeTree merge keeps one and deletes the other permanently, along with its spend.

    This is information-theoretically unavoidable for a span that carries no producer id: nothing
    is left to tell the two apart. It is the deliberate trade made in CTO-402 against the opposite
    failure (a retried batch duplicating spend at full batch scale, permanently, in the rollups).
    The realistic trigger is ordinary: a millisecond-resolution clock, two replicas making the same
    repeated call, and single-span flushes that put every span at batch position 0.

    If this assertion ever flips to `!=`, the collapse is gone and so is the retry fix; if the
    derivation is changed on purpose, change limitation 4 in db/clickhouse/otel_spans.sql and the
    :func:`gateway.mapping._derive_span_ids` docstring in the same commit.
    """
    one = {GenAI.SYSTEM: "openai", GenAI.OPERATION_NAME: "embedding", GenAI.USAGE_INPUT_TOKENS: 7}
    two = dict(one)  # a different call that happens to look exactly the same
    assert _ids(one, ts_ns=1_700_000_000_000_000_000, batch_index=0) == _ids(
        two, ts_ns=1_700_000_000_000_000_000, batch_index=0
    )


def test_known_collapse_end_to_end_two_single_span_batches_become_one_key() -> None:
    """The same known collapse, measured the way it actually bites: two separate single-span posts.

    Each call is alone in its own batch, so both sit at batch position 0, and a coarse client clock
    gives both the same timestamp. Two real calls, two real costs, ONE surviving row after a merge.
    Pinned rather than hidden: see otel_spans.sql limitation 4.
    """
    ts_ns = 1_700_000_000_000_000_000  # identical to the nanosecond, as a ms-resolution clock gives
    span = _idless_span(11, ts_ns)

    with _client() as (c, store):
        assert _post(c, "t-cto402-collapse", [dict(span)]).status_code == 200
        assert _post(c, "t-cto402-collapse", [dict(span)]).status_code == 200
        assert len(store.spans) == 2  # both were written
        assert len({_sorting_key(r) for r in store.spans}) == 1  # one survives the merge


# --- the id is hashed from the POSTED span, not the enriched one ----------------------------------


def _enriched(span: dict, *, catalog_version: str, cost_micro: int) -> dict:
    """What ``enrich_cost`` hands back: a COPY of the span carrying server-side cost fields.

    Built by hand rather than by calling the real enricher so the test states exactly which keys
    move between a first attempt and its retry. Those two keys are the whole point of CTO-402's
    second finding.
    """
    out = dict(span)
    out[GenAI.COST_ESTIMATED_MICRO_USD] = cost_micro
    out[GenAI.COST_PRICE_CATALOG_VERSION] = catalog_version
    out[GenAI.COST_CURRENCY] = "USD"
    return out


def test_derived_id_survives_a_price_catalog_reload_between_attempt_and_retry() -> None:
    """A catalog reload between an attempt and its retry must NOT move the derived id.

    The rows are written from the enriched attributes, but the identity is hashed from the posted
    span, so re-pricing changes the stored cost without changing what the engine collapses on.
    """
    posted = {GenAI.SYSTEM: "openai", GenAI.OPERATION_NAME: "chat", GenAI.USAGE_INPUT_TOKENS: 42}
    attempt = _enriched(posted, catalog_version="2026-09-01", cost_micro=1200)
    retry = _enriched(posted, catalog_version="2026-09-15", cost_micro=1350)

    assert _ids(attempt, identity_span=posted) == _ids(retry, identity_span=posted)


def test_hashing_the_enriched_span_would_break_that_and_this_test_proves_it() -> None:
    """The negative half of the test above, so the fix cannot be silently reverted.

    Without ``identity_span`` the enriched dict is what gets hashed, and the two attempts derive
    DIFFERENT ids purely because the price catalog version moved. If someone changes app.py back to
    passing ``result.attributes`` as the identity, the test above starts failing and this one
    explains why.
    """
    posted = {GenAI.SYSTEM: "openai", GenAI.OPERATION_NAME: "chat", GenAI.USAGE_INPUT_TOKENS: 42}
    attempt = _enriched(posted, catalog_version="2026-09-01", cost_micro=1200)
    retry = _enriched(posted, catalog_version="2026-09-15", cost_micro=1350)

    assert _ids(attempt) != _ids(retry)


def test_a_key_the_mapper_drops_still_influences_the_derived_id() -> None:
    """Documents the cost of hashing the POSTED span: dropped keys are still in the digest.

    ``gen_ai.account_label`` is wire-only and never reaches ClickHouse, so these two spans store
    byte-identical rows, yet they derive different ids. That direction is the safe one (it
    separates rather than collapses), and it is stated in the _derive_span_ids docstring.
    """
    base = {GenAI.SYSTEM: "openai", GenAI.USAGE_INPUT_TOKENS: 5}
    with_label = dict(base)
    with_label[GenAI.ACCOUNT_LABEL] = "acme-corp"

    assert _ids(dict(base)) != _ids(dict(with_label))
    assert _ids(dict(with_label)) == _ids(dict(with_label))  # still deterministic


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
