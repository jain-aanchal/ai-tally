# SPDX-License-Identifier: Apache-2.0
"""CTO-417: a subscription-billed span must never be priced at API list rates.

The gateway prices from (provider, model, tokens) and had no notion of HOW a call was billed, so a
call covered by a seat or plan whose model happens to sit in the catalog was priced at list rates
and stamped ``CostSource = 'estimated'``, asserting a cost the customer never incurred.

Production evidence from a pilot tenant over 90 days: 181 subscription
calls escaped pricing only because their model ids missed the catalog, and one openai / gpt-4o-mini
call did not miss. That call, 12 input tokens and 5 output, is reproduced here end to end through
POST /v1/batches into a fake ClickHouse, so the assertions are about the row that really gets
written rather than about an intermediate dict.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from tally.schema import GenAI

from gateway import app as app_module
from gateway.app import app
from gateway.config import get_settings
from gateway.mapping import COLUMNS, span_to_row


class FakeStore:
    def __init__(self) -> None:
        self.spans: list[tuple] = []
        self._lock = threading.Lock()

    def insert_spans(self, rows: list[tuple]) -> int:
        with self._lock:
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
def _client(store: FakeStore) -> Iterator[TestClient]:
    settings = get_settings()
    prev_buffered = settings.ingest_buffered
    settings.ingest_buffered = False
    orig_factory = app_module.ClickHouseStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    try:
        with TestClient(app) as c:
            app.state.settings.require_api_key = False
            yield c
    finally:
        app_module.ClickHouseStore = orig_factory  # type: ignore[assignment]
        settings.ingest_buffered = prev_buffered


def _post(c: TestClient, spans: list[dict]) -> dict:
    r = c.post(
        "/v1/batches",
        json={"tenant_id": "t-local", "sdk_version": "test", "resource_spans": spans},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _row(store: FakeStore, index: int = 0) -> dict[str, object]:
    return dict(zip(COLUMNS, store.spans[index], strict=True))


def _real_call(**over: object) -> dict:
    """The production gpt-4o-mini call, attribute for attribute."""
    span: dict[str, object] = {
        "trace_id": "trace-1",
        "span_id": "span-1",
        GenAI.SYSTEM: "openai",
        GenAI.REQUEST_MODEL: "gpt-4o-mini",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 12,
        GenAI.USAGE_OUTPUT_TOKENS: 5,
        GenAI.FEATURE_TAG: "assistant",
    }
    span.update(over)
    return span


def test_unmarked_span_still_prices_exactly_as_before() -> None:
    """The additive-only contract (CTO-31): an OLD producer sends no mode and is unaffected.

    This is the shape every producer in the field posts today, and it is also the baseline the next
    test measures the fix against: 5 micro-USD, stamped estimated, with a catalog version.
    """
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_real_call()])
    row = _row(store)
    assert row["CostSource"] == "estimated"
    assert row["EstimatedCost"] is not None
    assert row["EstimatedCost"] > 0
    assert row["PriceCatalogVersion"] != ""
    assert GenAI.COST_BILLING_MODE not in row["SpanAttributes"]  # type: ignore[operator]


def test_subscription_marked_span_lands_with_no_cost() -> None:
    """The whole point: the catalog CAN price this model, and it is still not priced."""
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_real_call(**{GenAI.COST_BILLING_MODE: "subscription"})])
    row = _row(store)
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "subscription"
    assert row["PriceCatalogVersion"] == ""


def test_subscription_withholds_the_cost_and_nothing_else() -> None:
    """Token counts, model, feature tag and attribution all still land."""
    store = FakeStore()
    with _client(store) as c:
        _post(
            c,
            [
                _real_call(
                    **{
                        GenAI.COST_BILLING_MODE: "subscription",
                        GenAI.USAGE_CACHED_INPUT_TOKENS: 3,
                        GenAI.SESSION_ID: "sess-1",
                        GenAI.ACCOUNT_ID_HASH: "a" * 64,
                    }
                )
            ],
        )
    row = _row(store)
    assert row["InputTokens"] == 12
    assert row["OutputTokens"] == 5
    assert row["CachedInputTokens"] == 3
    assert row["GenAiRequestModel"] == "gpt-4o-mini"
    assert row["GenAiSystem"] == "openai"
    assert row["FeatureTag"] == "assistant"
    assert row["SessionId"] == "sess-1"
    assert row["AccountIdHash"] == "a" * 64
    # The marker itself is queryable from the long tail, so a reader can see WHY without having to
    # infer it from CostSource alone.
    assert row["SpanAttributes"][GenAI.COST_BILLING_MODE] == "subscription"  # type: ignore[index]


def test_one_batch_mixing_both_modes_is_treated_per_span_not_per_batch() -> None:
    """The motivating tenant mixes billing modes under one tenant, often in one process.

    Their Fireworks traffic is real per-token API spend while their openai and claude-code traffic
    is subscription, so a decision taken once per batch (or per tenant, or per provider) would be
    wrong for one half of it whichever way it went.
    """
    store = FakeStore()
    with _client(store) as c:
        _post(
            c,
            [
                _real_call(trace_id="t-sub", span_id="s-sub", **{
                    GenAI.COST_BILLING_MODE: "subscription"
                }),
                _real_call(trace_id="t-api", span_id="s-api"),
                _real_call(trace_id="t-exp", span_id="s-exp", **{GenAI.COST_BILLING_MODE: "api"}),
            ],
        )
    assert len(store.spans) == 3
    subscription, unmarked, explicit_api = (_row(store, i) for i in range(3))

    assert subscription["CostSource"] == "subscription"
    assert subscription["EstimatedCost"] is None

    assert unmarked["CostSource"] == "estimated"
    assert unmarked["EstimatedCost"] is not None and unmarked["EstimatedCost"] > 0

    # An explicit "api" must price identically to saying nothing at all.
    assert explicit_api["CostSource"] == "estimated"
    assert explicit_api["EstimatedCost"] == unmarked["EstimatedCost"]


def test_unrecognised_mode_is_unpriced_rather_than_list_priced() -> None:
    """Refusing to price beats guessing; and it is not labelled subscription, which we were not told."""
    store = FakeStore()
    with _client(store) as c:
        _post(c, [_real_call(**{GenAI.COST_BILLING_MODE: "prepaid-credits"})])
    row = _row(store)
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "unpriced"


# --- the mapper's own guard, independent of enrichment -------------------------------------------


def test_mapper_refuses_a_cost_on_a_subscription_span_by_itself() -> None:
    """span_to_row is the storage boundary and is the LAST place a cost can be written.

    Callers that map a span without going through enrich_cost exist (gateway/connectors/base.py),
    so this guard does not lean on the enrichment one. Here a cost is present on the span and must
    still be withheld.
    """
    row = dict(
        zip(
            COLUMNS,
            span_to_row(
                {
                    GenAI.COST_ESTIMATED_MICRO_USD: 5,
                    GenAI.COST_BILLING_MODE: "subscription",
                    GenAI.USAGE_INPUT_TOKENS: 12,
                },
                tenant_id="t1",
                effective_ts_ns=0,
            ),
            strict=True,
        )
    )
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "subscription"
    assert row["InputTokens"] == 12


def test_mapper_leaves_an_unmarked_span_alone() -> None:
    row = dict(
        zip(
            COLUMNS,
            span_to_row({GenAI.COST_ESTIMATED_MICRO_USD: 5}, tenant_id="t1", effective_ts_ns=0),
            strict=True,
        )
    )
    assert row["CostSource"] == "estimated"
    assert row["EstimatedCost"] is not None
