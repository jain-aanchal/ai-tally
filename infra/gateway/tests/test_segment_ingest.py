"""Unit tests for the Segment ingest worker (CTO-127).

No network, no DB: the HTTP client, credential store/resolver, ClickHouse store and integration
store are all in-memory fakes (see ``_worker_fakes``).
"""

from __future__ import annotations

from _worker_fakes import (
    FakeCHStore,
    FakeHttp,
    FakeIntegrations,
    FakeResolver,
    FakeSecretStore,
    make_secret,
)

from gateway.segment_ingest import SegmentWorker, map_identify_event, map_track_event
from tally.hmac_keys import HmacKeyRegistry

T = "t-acme"


def _identity(v: str) -> str:
    return f"h_{v}"


# --- pure mappers -------------------------------------------------------------------------------


def test_track_with_revenue_maps_to_monetary_micro_usd() -> None:
    ev = map_track_event(
        {
            "type": "track",
            "event": "Order Completed",
            "messageId": "m-1",
            "userId": "u-1",
            "timestamp": "2026-07-01T00:00:00.000Z",
            "properties": {"revenue": 49.0, "currency": "usd"},
        },
        _identity,
    )
    assert ev is not None
    assert ev.event_name == "Order Completed"
    assert ev.value_amount_micro == 49_000_000  # 49.0 × 1_000_000
    assert ev.value_type == "monetary"
    assert ev.value_currency == "USD"
    assert ev.business_event_id == "m-1"
    assert ev.user_id_hash == "h_u-1"
    assert ev.source == "segment"


def test_track_without_revenue_is_a_count_touch() -> None:
    ev = map_track_event(
        {"type": "track", "event": "Viewed Page", "messageId": "m-2", "anonymousId": "a-2"},
        _identity,
    )
    assert ev is not None
    assert ev.value_type == "count"
    assert ev.value_amount_micro is None
    assert ev.user_id_hash == "h_a-2"  # falls back to anonymousId


def test_track_requires_message_id() -> None:
    assert map_track_event({"type": "track", "event": "x"}, _identity) is None


def test_non_track_is_ignored_by_track_mapper() -> None:
    assert map_track_event({"type": "identify", "userId": "u"}, _identity) is None


def test_identify_links_anonymous_to_user() -> None:
    link = map_identify_event(
        {"type": "identify", "userId": "u-1", "anonymousId": "a-1", "timestamp": "2026-07-01T00:00:00Z"},
        _identity,
    )
    assert link is not None
    assert link.identity_a == "h_a-1"
    assert link.identity_a_type == "anonymous_id"
    assert link.identity_b == "h_u-1"
    assert link.identity_b_type == "user_id"
    assert link.source == "segment"


def test_identify_needs_both_sides() -> None:
    assert map_identify_event({"type": "identify", "userId": "u-1"}, _identity) is None
    assert map_identify_event({"type": "identify", "anonymousId": "a-1"}, _identity) is None


# --- worker cycle -------------------------------------------------------------------------------


def _worker(http: FakeHttp, integrations: FakeIntegrations, ch: FakeCHStore, *, secret=True):
    return SegmentWorker(
        secrets=FakeSecretStore(make_secret("segment") if secret else None),
        resolver=FakeResolver({"ref-1": "wk_live_abc"}),
        http=http,
        store=ch,
        integrations=integrations,
        registry=HmacKeyRegistry(),
    )


def test_cycle_writes_events_and_links_and_records_success() -> None:
    payload = {
        "events": [
            {
                "type": "track",
                "event": "Order Completed",
                "messageId": "m-1",
                "userId": "u-1",
                "properties": {"revenue": 10.0},
            },
            {"type": "identify", "userId": "u-1", "anonymousId": "a-1"},
            {"type": "page", "userId": "u-1"},  # ignored
        ]
    }
    http, integrations, ch = FakeHttp(payload), FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)

    assert result.status == "success"
    assert result.event_count == 2  # 1 track + 1 identify link
    assert result.recorded is True
    assert len(ch.events) == 1
    assert len(ch.links) == 1
    call = integrations.calls[0]
    assert call["connector_id"] == "segment"
    assert call["status"] == "success"
    assert call["event_count"] == 2
    # The resolved write-key was used as the bearer token, never the raw ref.
    assert http.calls[0]["headers"]["Authorization"] == "Bearer wk_live_abc"


def test_cycle_skips_when_not_connected() -> None:
    http, integrations, ch = FakeHttp({"events": []}), FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch, secret=False).run_cycle(T)
    assert result.status == "skipped"
    assert integrations.calls == []
    assert http.calls == []  # never even fetched


def test_cycle_records_failed_on_http_error_with_pii_scrubbed() -> None:
    http = FakeHttp(error=RuntimeError("timeout contacting foo@example.com"))
    integrations, ch = FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)

    assert result.status == "failed"
    assert result.event_count == 0
    assert ch.events == []
    msg = integrations.calls[0]["error_message"]
    assert "foo@example.com" not in msg
    assert "[redacted-email]" in msg


def test_cycle_records_failed_on_credential_resolution_failure() -> None:
    worker = SegmentWorker(
        secrets=FakeSecretStore(make_secret("segment", ref="missing-ref")),
        resolver=FakeResolver({}),  # can't resolve
        http=FakeHttp({"events": []}),
        store=FakeCHStore(),
        integrations=(integrations := FakeIntegrations()),
        registry=HmacKeyRegistry(),
    )
    result = worker.run_cycle(T)
    assert result.status == "failed"
    assert "credential resolution failed" in (integrations.calls[0]["error_message"] or "")


def test_cycle_is_partial_when_an_item_is_malformed() -> None:
    payload = {"events": [{"type": "track", "event": "ok", "messageId": "m-1"}, "not-a-dict"]}
    http, integrations, ch = FakeHttp(payload), FakeIntegrations(), FakeCHStore()
    result = _worker(http, integrations, ch).run_cycle(T)
    assert result.status == "partial"
    assert result.event_count == 1


def test_record_run_failure_is_swallowed() -> None:
    http = FakeHttp({"events": []})
    integrations = FakeIntegrations(raise_on_record=True)
    result = _worker(http, integrations, FakeCHStore()).run_cycle(T)
    # Cycle still returns; record simply didn't stick.
    assert result.status == "success"
    assert result.recorded is False
