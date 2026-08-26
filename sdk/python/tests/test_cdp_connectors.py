# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.cdp_connectors (CTO-68): CDP/revenue webhook connectors."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tally.cdp_connectors import (
    BusinessEvent,
    ConnectorRegistry,
    EventDeduplicator,
    GenericRevenueConnector,
    HubSpotConnector,
    RevenuePayloadError,
    RudderstackConnector,
    SegmentConnector,
    StripeConnector,
    WebhookIngestor,
    default_registry,
)
from tally.identity import IdentityGraph

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# BusinessEvent
# --------------------------------------------------------------------------- #
def test_business_event_validation():
    now = datetime(2026, 5, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        BusinessEvent("", "t1", "segment", "e", now)
    with pytest.raises(ValueError):
        BusinessEvent("id", "", "segment", "e", now)
    with pytest.raises(ValueError):
        BusinessEvent("id", "t1", "segment", "e", now, value_micro_usd=-1)
    with pytest.raises(ValueError):
        BusinessEvent("id", "t1", "segment", "e", now, value_micro_usd=True)


def test_business_event_is_revenue_and_as_dict():
    now = datetime(2026, 5, 1, tzinfo=UTC)
    e = BusinessEvent("id", "t1", "segment", "Purchase", now, value_micro_usd=5_000_000)
    assert e.is_revenue is True
    d = e.as_dict()
    assert d["value_micro_usd"] == 5_000_000
    assert d["event_name"] == "Purchase"
    assert BusinessEvent("id", "t1", "segment", "Signup", now).is_revenue is False


# --------------------------------------------------------------------------- #
# Segment
# --------------------------------------------------------------------------- #
def test_segment_track_with_revenue():
    c = SegmentConnector()
    payload = {
        "type": "track",
        "messageId": "m1",
        "event": "Order Completed",
        "timestamp": "2026-05-01T12:00:00Z",
        "userId": "u1",
        "anonymousId": "a1",
        "properties": {"revenue": 49.99, "currency": "USD"},
    }
    res = c.parse("t1", payload)
    assert len(res.business_events) == 1
    ev = res.business_events[0]
    assert ev.business_event_id == "m1"
    assert ev.event_name == "Order Completed"
    assert ev.value_micro_usd == 49_990_000
    assert ev.user_id == "u1"
    assert ev.anonymous_id == "a1"
    assert ev.occurred_at == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_segment_track_without_revenue_is_zero_value():
    c = SegmentConnector()
    res = c.parse(
        "t1",
        {
            "type": "track",
            "messageId": "m2",
            "event": "Signed Up",
            "timestamp": "2026-05-01T00:00:00Z",
        },
    )
    assert res.business_events[0].value_micro_usd == 0
    assert res.business_events[0].is_revenue is False


def test_segment_identify_makes_identify_event():
    c = SegmentConnector()
    res = c.parse(
        "t1",
        {
            "type": "identify",
            "messageId": "m3",
            "userId": "u1",
            "anonymousId": "a1",
            "timestamp": "2026-05-01T00:00:00Z",
        },
    )
    assert len(res.identifies) == 1
    assert res.identifies[0].user_id == "u1"
    assert res.identifies[0].anonymous_id == "a1"
    assert res.business_events == ()


def test_segment_alias_makes_alias_event():
    c = SegmentConnector()
    res = c.parse(
        "t1",
        {
            "type": "alias",
            "messageId": "m4",
            "userId": "u1",
            "previousId": "a1",
            "timestamp": "2026-05-01T00:00:00Z",
        },
    )
    assert len(res.aliases) == 1
    assert res.aliases[0].previous_id == "a1"
    assert res.aliases[0].new_id == "u1"


def test_segment_page_type_yields_nothing():
    c = SegmentConnector()
    res = c.parse("t1", {"type": "page", "messageId": "m5", "timestamp": "2026-05-01T00:00:00Z"})
    assert res.is_empty


def test_segment_junk_payload_is_empty():
    c = SegmentConnector()
    assert c.parse("t1", {}).is_empty
    assert c.parse("t1", {"type": "track"}).is_empty  # no id/timestamp
    assert c.parse("t1", "not a dict").is_empty  # type: ignore[arg-type]


def test_rudderstack_shares_segment_spec_with_own_source():
    c = RudderstackConnector()
    res = c.parse(
        "t1",
        {
            "type": "track",
            "messageId": "m1",
            "event": "Converted",
            "timestamp": "2026-05-01T00:00:00Z",
            "properties": {"revenue": 10},
        },
    )
    assert res.business_events[0].source == "rudderstack"
    assert res.business_events[0].value_micro_usd == 10_000_000


# --------------------------------------------------------------------------- #
# Stripe
# --------------------------------------------------------------------------- #
def test_stripe_invoice_paid_revenue_from_cents():
    c = StripeConnector()
    payload = {
        "id": "evt_1",
        "type": "invoice.paid",
        "created": 1_777_000_000,  # epoch seconds
        "data": {"object": {"amount_paid": 2500, "currency": "usd", "customer": "cus_1"}},
    }
    res = c.parse("t1", payload)
    ev = res.business_events[0]
    assert ev.business_event_id == "evt_1"
    assert ev.value_micro_usd == 2500 * 10_000  # 25.00 USD
    assert ev.currency == "USD"
    assert ev.user_id == "cus_1"
    assert ev.occurred_at.tzinfo is UTC


def test_stripe_non_revenue_event_zero_value():
    c = StripeConnector()
    res = c.parse(
        "t1",
        {
            "id": "evt_2",
            "type": "customer.created",
            "created": 1_777_000_000,
            "data": {"object": {"id": "cus_1"}},
        },
    )
    assert res.business_events[0].value_micro_usd == 0


def test_stripe_charge_succeeded_uses_amount():
    c = StripeConnector()
    res = c.parse(
        "t1",
        {
            "id": "evt_3",
            "type": "charge.succeeded",
            "created": 1_777_000_000,
            "data": {"object": {"amount": 999, "currency": "usd"}},
        },
    )
    assert res.business_events[0].value_micro_usd == 999 * 10_000


def test_stripe_junk_is_empty():
    c = StripeConnector()
    assert c.parse("t1", {}).is_empty
    assert c.parse("t1", {"id": "evt", "type": "invoice.paid"}).is_empty  # no created


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #
def test_hubspot_deal_amount():
    c = HubSpotConnector()
    payload = {
        "eventId": "h1",
        "subscriptionType": "deal.propertyChange",
        "occurredAt": 1_777_000_000_000,  # epoch ms
        "objectId": "deal_1",
        "properties": {"amount": "1200.50"},
    }
    res = c.parse("t1", payload)
    ev = res.business_events[0]
    assert ev.business_event_id == "h1"
    assert ev.value_micro_usd == 1_200_500_000
    assert ev.user_id == "deal_1"
    assert ev.occurred_at.tzinfo is UTC


def test_hubspot_junk_is_empty():
    c = HubSpotConnector()
    assert c.parse("t1", {}).is_empty
    assert c.parse("t1", {"eventId": "h1"}).is_empty  # no occurredAt


# --------------------------------------------------------------------------- #
# Deduplicator
# --------------------------------------------------------------------------- #
def test_deduplicator_marks_and_detects():
    d = EventDeduplicator()
    assert d.mark("t1", "e1") is True
    assert d.mark("t1", "e1") is False  # replay
    assert d.is_duplicate("t1", "e1") is True
    assert d.is_duplicate("t1", "e2") is False
    assert d.count("t1") == 1


def test_deduplicator_is_tenant_scoped():
    d = EventDeduplicator()
    d.mark("t1", "e1")
    assert d.is_duplicate("t2", "e1") is False  # different tenant


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_default_registry_has_the_webhook_sources_plus_the_revenue_api():
    reg = default_registry()
    assert reg.sources == ("hubspot", "revenue-api", "rudderstack", "segment", "stripe")
    assert reg.get("segment") is not None
    assert reg.get("unknown") is None


def test_registry_register():
    reg = ConnectorRegistry()
    reg.register(SegmentConnector())
    assert reg.get("segment") is not None


# --------------------------------------------------------------------------- #
# Ingestor — idempotency, late/replay, routing
# --------------------------------------------------------------------------- #
def _track(mid, revenue, ts="2026-05-01T00:00:00Z"):
    return {
        "type": "track",
        "messageId": mid,
        "event": "Purchase",
        "timestamp": ts,
        "properties": {"revenue": revenue},
    }


def test_ingest_accepts_then_dedupes_replays():
    ing = WebhookIngestor()
    r1 = ing.ingest("segment", "t1", _track("m1", 10))
    assert r1.accepted_count == 1
    assert r1.total_value_micro_usd == 10_000_000
    # exact replay (same messageId) is dropped
    r2 = ing.ingest("segment", "t1", _track("m1", 10))
    assert r2.accepted_count == 0
    assert r2.duplicates == 1


def test_ingest_late_arriving_event_still_accepted_once():
    # A late event (old occurred_at) is accepted; only a duplicate id is dropped.
    ing = WebhookIngestor()
    late = _track("late1", 5, ts="2026-04-01T00:00:00Z")
    r = ing.ingest("segment", "t1", late)
    assert r.accepted_count == 1
    assert r.accepted[0].occurred_at == datetime(2026, 4, 1, tzinfo=UTC)
    # redelivery of the same late event is a no-op
    assert ing.ingest("segment", "t1", late).accepted_count == 0


def test_ingest_unknown_source_is_empty():
    ing = WebhookIngestor()
    r = ing.ingest("mailchimp", "t1", {"foo": "bar"})
    assert r.accepted_count == 0
    assert r.duplicates == 0


def test_ingest_empty_tenant_is_empty():
    ing = WebhookIngestor()
    assert ing.ingest("segment", "", _track("m1", 1)).accepted_count == 0


def test_ingest_routes_identity_events_through():
    ing = WebhookIngestor()
    r = ing.ingest(
        "segment",
        "t1",
        {
            "type": "identify",
            "messageId": "m1",
            "userId": "u1",
            "anonymousId": "a1",
            "timestamp": "2026-05-01T00:00:00Z",
        },
    )
    assert len(r.identifies) == 1
    assert r.accepted_count == 0


def test_ingest_result_as_dict():
    ing = WebhookIngestor()
    r = ing.ingest("segment", "t1", _track("m1", 3))
    d = r.as_dict()
    assert d["accepted_count"] == 1
    assert d["total_value_micro_usd"] == 3_000_000


def test_ingest_identity_events_feed_identity_graph():
    # End-to-end: an identify webhook populates the real identity graph, linking
    # the anonymous id to the user id so a later conversion can reach back.
    ing = WebhookIngestor()
    graph = IdentityGraph()
    r = ing.ingest(
        "segment",
        "t1",
        {
            "type": "identify",
            "messageId": "m1",
            "userId": "u1",
            "anonymousId": "a1",
            "timestamp": "2026-05-01T00:00:00Z",
        },
    )
    for ev in r.identifies:
        graph.ingest_identify("t1", ev)
    resolved = graph.resolve_identity("t1", "a1")
    assert "u1" in resolved


def test_ingest_stripe_revenue_end_to_end():
    ing = WebhookIngestor()
    payload = {
        "id": "evt_1",
        "type": "invoice.paid",
        "created": 1_777_000_000,
        "data": {"object": {"amount_paid": 10_000, "currency": "usd", "customer": "cus_1"}},
    }
    r = ing.ingest("stripe", "t1", payload)
    assert r.accepted_count == 1
    assert r.total_value_micro_usd == 10_000 * 10_000  # $100
    # Stripe re-sends the same event id on retry -> deduped
    assert ing.ingest("stripe", "t1", payload).accepted_count == 0


# --------------------------------------------------------------------------- #
# Generic revenue API (CTO-199)
# --------------------------------------------------------------------------- #
def _revenue(**overrides):
    payload = {
        "event_id": "inv_2026_08_0001",
        "account_id": "acct_northwind",
        "amount": "1499.00",
        "currency": "usd",
        "occurred_at": "2026-08-25T12:00:00Z",
        "event_name": "invoice_paid",
    }
    payload.update(overrides)
    return payload


def test_generic_revenue_parses_the_documented_shape():
    ev = GenericRevenueConnector().parse_strict("t1", _revenue())
    assert ev.business_event_id == "inv_2026_08_0001"
    assert ev.account_id == "acct_northwind"
    assert ev.value_micro_usd == 1_499_000_000
    assert ev.currency == "USD"  # normalized, never converted
    assert ev.value_type == "monetary"
    assert ev.source == "revenue-api"
    assert ev.occurred_at == datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    assert ev.user_id is None


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"event_id": ""}, "event_id"),
        ({"account_id": None}, "account_id"),
        ({"event_name": "  "}, "event_name"),
        ({"occurred_at": "yesterday"}, "occurred_at"),
        ({"amount": "not-a-number"}, "amount"),
        ({"amount": None}, "amount"),
        ({"amount": "-5"}, "non-negative"),
        ({"currency": "dollars"}, "currency"),
        ({"value_type": "revenue"}, "value_type"),
        ({"user_id": 7}, "user_id"),
        ({"properties": "a=b"}, "properties"),
        ({"value_type": "count", "amount": "10"}, "count"),
    ],
)
def test_generic_revenue_rejects_bad_payloads_by_field(overrides, fragment):
    with pytest.raises(RevenuePayloadError) as exc:
        GenericRevenueConnector().parse_strict("t1", _revenue(**overrides))
    assert fragment in str(exc.value)


def test_generic_revenue_parse_never_raises_for_the_shared_path():
    # The CDPConnector contract: a bad payload is an empty result, not an exception.
    assert GenericRevenueConnector().parse("t1", _revenue(amount="junk")).is_empty
    assert GenericRevenueConnector().parse("t1", ["not", "a", "mapping"]).is_empty


def test_generic_revenue_count_event_carries_no_money():
    ev = GenericRevenueConnector().parse_strict(
        "t1", _revenue(value_type="count", amount=None, event_name="seat_activated")
    )
    assert ev.value_type == "count"
    assert ev.value_micro_usd == 0


def test_generic_revenue_refund_is_a_positive_amount_with_a_refund_type():
    ev = GenericRevenueConnector().parse_strict(
        "t1", _revenue(value_type="refund", amount="99.50", event_name="refund")
    )
    assert ev.value_type == "refund"
    assert ev.value_micro_usd == 99_500_000


def test_generic_revenue_is_idempotent_on_the_caller_supplied_event_id():
    ing = WebhookIngestor()
    first = ing.ingest_revenue_api("t1", _revenue())
    assert first.accepted_count == 1
    # Same event_id, retried (and with a different amount; the id is what decides).
    retry = ing.ingest_revenue_api("t1", _revenue(amount="2000.00"))
    assert retry.accepted_count == 0
    assert retry.duplicates == 1
    # A different tenant with the same id is untouched.
    assert ing.ingest_revenue_api("t2", _revenue()).accepted_count == 1


def test_ingest_revenue_api_raises_on_a_payload_the_caller_must_fix():
    ing = WebhookIngestor()
    with pytest.raises(RevenuePayloadError):
        ing.ingest_revenue_api("t1", _revenue(event_id=None))


def test_forget_restores_a_retry_after_a_failed_write():
    ing = WebhookIngestor()
    assert ing.ingest_revenue_api("t1", _revenue()).accepted_count == 1
    # Pretend the durable write failed: the caller retries and must not be told "duplicate".
    assert ing.forget("t1", "inv_2026_08_0001") is True
    assert ing.ingest_revenue_api("t1", _revenue()).accepted_count == 1
    assert ing.forget("t1", "never-seen") is False


def test_business_event_rejects_an_unknown_value_type():
    with pytest.raises(ValueError):
        BusinessEvent(
            "id", "t1", "revenue-api", "e", datetime(2026, 5, 1, tzinfo=UTC), value_type="cash"
        )
