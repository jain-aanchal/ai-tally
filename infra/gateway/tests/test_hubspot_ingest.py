"""Unit tests for the HubSpot ingest worker (CTO-127). No network, no DB — all deps are fakes."""

from __future__ import annotations

from _worker_fakes import (
    FakeCHStore,
    FakeHttp,
    FakeIntegrations,
    FakeResolver,
    FakeSecretStore,
    make_secret,
)

from gateway.hubspot_ingest import HubSpotWorker, map_deal_stage_event
from tally.hmac_keys import HmacKeyRegistry

T = "t-acme"


def _identity(v: str) -> str:
    return f"h_{v}"


# --- pure mapper --------------------------------------------------------------------------------


def test_closed_won_maps_to_conversion_with_amount_micro_usd() -> None:
    ev = map_deal_stage_event(
        {
            "eventId": "evt-9",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "objectId": 123,
            "occurredAt": 1_700_000_000_000,
            "properties": {"amount": 5000, "email": "Buyer@Example.com"},
        },
        _identity,
    )
    assert ev is not None
    assert ev.event_name == "conversion"
    assert ev.value_amount_micro == 5_000_000_000  # 5000 × 1_000_000
    assert ev.value_type == "monetary"
    assert ev.business_event_id == "evt-9"
    assert ev.user_id_hash == "h_buyer@example.com"  # email lowercased before hashing
    assert ev.source == "hubspot"


def test_non_closed_won_stage_change_is_ignored() -> None:
    assert (
        map_deal_stage_event(
            {"propertyName": "dealstage", "propertyValue": "presentationscheduled"}, _identity
        )
        is None
    )


def test_non_dealstage_property_change_is_ignored() -> None:
    assert (
        map_deal_stage_event(
            {"propertyName": "amount", "propertyValue": "closedwon"}, _identity
        )
        is None
    )


def test_missing_email_yields_empty_user_hash() -> None:
    ev = map_deal_stage_event(
        {
            "eventId": "evt-1",
            "propertyName": "dealstage",
            "propertyValue": "closed won",
            "properties": {"amount": 100},
        },
        _identity,
    )
    assert ev is not None
    assert ev.user_id_hash == ""  # honest unattributed


def test_falls_back_to_deterministic_id_without_event_id() -> None:
    ev = map_deal_stage_event(
        {"propertyName": "dealstage", "propertyValue": "closedwon", "objectId": 77}, _identity
    )
    assert ev is not None
    assert ev.business_event_id == "hubspot-deal-77-closedwon"


# --- worker cycle -------------------------------------------------------------------------------


def _worker(http: FakeHttp, integrations: FakeIntegrations, ch: FakeCHStore, *, secret=True):
    return HubSpotWorker(
        secrets=FakeSecretStore(make_secret("hubspot") if secret else None),
        resolver=FakeResolver({"ref-1": "oauth-token"}),
        http=http,
        store=ch,
        integrations=integrations,
        registry=HmacKeyRegistry(),
    )


def test_cycle_writes_only_closed_won_conversions() -> None:
    payload = {
        "results": [
            {
                "eventId": "e1",
                "propertyName": "dealstage",
                "propertyValue": "closedwon",
                "properties": {"amount": 200},
            },
            {"eventId": "e2", "propertyName": "dealstage", "propertyValue": "qualifiedtobuy"},
        ]
    }
    http, integrations, ch = FakeHttp(payload), FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)
    assert result.status == "success"
    assert result.event_count == 1
    assert len(ch.events) == 1
    assert ch.events[0].value_amount_micro == 200_000_000
    assert http.calls[0]["headers"]["Authorization"] == "Bearer oauth-token"


def test_cycle_skips_when_not_connected() -> None:
    http = FakeHttp({"results": []})
    result = _worker(http, FakeIntegrations(), FakeCHStore(), secret=False).run_cycle(T)
    assert result.status == "skipped"
    assert http.calls == []


def test_cycle_records_failed_on_http_error() -> None:
    http = FakeHttp(error=RuntimeError("500 from hubspot"))
    integrations, ch = FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)
    assert result.status == "failed"
    assert integrations.calls[0]["status"] == "failed"
    assert ch.events == []
