"""Unit tests for the Pendo ingest worker (CTO-127). No network, no DB — all deps are fakes."""

from __future__ import annotations

from _worker_fakes import (
    FakeCHStore,
    FakeHttp,
    FakeIntegrations,
    FakeResolver,
    FakeSecretStore,
    make_secret,
)

from gateway.pendo_ingest import PendoWorker, map_feature_first_use
from tally.hmac_keys import HmacKeyRegistry

T = "t-acme"


def _identity(v: str) -> str:
    return f"h_{v}"


# --- pure mapper --------------------------------------------------------------------------------


def test_feature_first_use_maps_to_low_value_count_touch() -> None:
    ev = map_feature_first_use(
        {"visitorId": "v-1", "featureId": "feat-1", "firstTime": 1_700_000_000_000},
        _identity,
    )
    assert ev is not None
    assert ev.event_name == "feature_first_used"
    assert ev.value_type == "count"
    assert ev.value_amount_micro is None  # low-value engagement touch, never fabricated revenue
    assert ev.user_id_hash == "h_v-1"
    assert ev.source == "pendo"
    # BusinessEventId embeds the HASH, not the raw visitor id (which may be an email).
    assert ev.business_event_id == "pendo-firstuse-feat-1-h_v-1"
    assert "v-1" not in ev.business_event_id.replace("h_v-1", "")


def test_missing_fields_yield_none() -> None:
    assert map_feature_first_use({"visitorId": "v", "featureId": "f"}, _identity) is None
    assert map_feature_first_use({"featureId": "f", "firstTime": 1}, _identity) is None
    assert map_feature_first_use({"visitorId": "v", "firstTime": 1}, _identity) is None


def test_snake_case_field_aliases_are_accepted() -> None:
    ev = map_feature_first_use(
        {"visitor_id": "v-2", "feature_id": "f-2", "first_time": 1_700_000_000_000}, _identity
    )
    assert ev is not None
    assert ev.user_id_hash == "h_v-2"


# --- worker cycle -------------------------------------------------------------------------------


def _worker(http: FakeHttp, integrations: FakeIntegrations, ch: FakeCHStore, *, secret=True):
    return PendoWorker(
        secrets=FakeSecretStore(make_secret("pendo") if secret else None),
        resolver=FakeResolver({"ref-1": "pendo-key"}),
        http=http,
        store=ch,
        integrations=integrations,
        registry=HmacKeyRegistry(),
    )


def test_cycle_writes_touches_and_records_success() -> None:
    payload = {
        "results": [
            {"visitorId": "v-1", "featureId": "f-1", "firstTime": 1_700_000_000_000},
            {"visitorId": "v-2", "featureId": "f-1", "firstTime": 1_700_000_100_000},
        ]
    }
    http, integrations, ch = FakeHttp(payload), FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)
    assert result.status == "success"
    assert result.event_count == 2
    assert len(ch.events) == 2
    assert all(e.value_type == "count" for e in ch.events)
    # Pendo authenticates via its integration-key header, not a bearer token.
    assert http.calls[0]["headers"]["x-pendo-integration-key"] == "pendo-key"


def test_cycle_skips_when_not_connected() -> None:
    http = FakeHttp({"results": []})
    result = _worker(http, FakeIntegrations(), FakeCHStore(), secret=False).run_cycle(T)
    assert result.status == "skipped"
    assert http.calls == []


def test_cycle_records_failed_on_http_error() -> None:
    http = FakeHttp(error=RuntimeError("pendo unreachable"))
    integrations, ch = FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)
    assert result.status == "failed"
    assert ch.events == []
