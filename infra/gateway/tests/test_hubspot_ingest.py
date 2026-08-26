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
from gateway.integration_workers import build_hasher
from tally.account_identity import AccountLinker
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
    # CTO-195: no company association on this payload, so the deal id is the fallback account.
    assert ev.account_id_hash == "h_123"


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


# --- account identity (CTO-195) -----------------------------------------------------------------


def test_associated_company_becomes_the_account() -> None:
    """A company is the account. A deal is one contract with it, so the company wins."""
    ev = map_deal_stage_event(
        {
            "eventId": "evt-10",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "objectId": 123,
            "properties": {"amount": 5000, "associatedcompanyid": 4242, "email": "b@e.com"},
        },
        _identity,
    )
    assert ev is not None
    assert ev.account_id_hash == "h_4242"
    assert ev.user_id_hash == "h_b@e.com"  # unchanged: this adds a column, not a reinterpretation


def test_account_is_hashed_never_raw() -> None:
    """Uses the real per-tenant HMAC path, not the fake hasher: no raw company id reaches a row."""
    hasher = build_hasher(HmacKeyRegistry(), T)
    ev = map_deal_stage_event(
        {
            "eventId": "evt-11",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "properties": {"amount": 1, "associatedcompanyid": "co_9"},
        },
        hasher,
    )
    assert ev is not None
    assert "co_9" not in ev.account_id_hash
    assert len(ev.account_id_hash) == 64


def test_no_identifiers_at_all_yields_an_empty_account() -> None:
    ev = map_deal_stage_event(
        {
            "eventId": "evt-12",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "properties": {"amount": 1},
        },
        _identity,
    )
    assert ev is not None
    assert ev.account_id_hash == ""  # honest unattributed, never a guess


def test_linker_infers_the_company_for_a_deal_that_names_only_a_contact() -> None:
    linker = AccountLinker()
    linker.observe(T, "h_buyer@example.com", "h_4242", source="hubspot")
    ev = map_deal_stage_event(
        {
            "eventId": "evt-13",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "properties": {"amount": 1, "email": "buyer@example.com"},
        },
        _identity,
        linker=linker,
        tenant_id=T,
    )
    assert ev is not None
    assert ev.account_id_hash == "h_4242"


def test_a_contact_seen_under_two_companies_attributes_nothing() -> None:
    """docs/cost-per-customer-plan.md: one user, one account. Do not guess between two."""
    linker = AccountLinker()
    common = {"propertyName": "dealstage", "propertyValue": "closedwon"}

    first = map_deal_stage_event(
        {**common, "eventId": "a", "properties": {"amount": 1, "associatedcompanyid": "co_1",
                                                  "email": "buyer@example.com"}},
        _identity, linker=linker, tenant_id=T,
    )
    second = map_deal_stage_event(
        {**common, "eventId": "b", "properties": {"amount": 1, "associatedcompanyid": "co_2",
                                                  "email": "buyer@example.com"}},
        _identity, linker=linker, tenant_id=T,
    )
    assert first is not None and second is not None
    # Both deals keep the company their own payload stated, which is not a guess.
    assert (first.account_id_hash, second.account_id_hash) == ("h_co_1", "h_co_2")
    # But the contact is now ambiguous, so nothing is inferred for them again.
    assert len(linker.conflicts(T)) == 1
    orphan = map_deal_stage_event(
        {**common, "eventId": "c", "properties": {"amount": 1, "email": "buyer@example.com"}},
        _identity, linker=linker, tenant_id=T,
    )
    assert orphan is not None
    assert orphan.account_id_hash == ""


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
