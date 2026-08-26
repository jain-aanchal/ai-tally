"""Segment ingest worker → business_events (track) + identity_graph (identify) (CTO-127).

Segment is a CDP: it emits ``track`` events (a user did something, sometimes with a revenue value)
and ``identify`` events (this anonymous visitor is this known user). This worker pulls a batch of
those events for a tenant over an injected HTTP client, authenticated with the tenant's source
write-key (resolved by reference), and maps them:

* ``track``    → a :class:`~tally.wire.BusinessEvent` (``source="segment"``). ``properties.revenue``
  (or ``properties.value``), when present, becomes a monetary value in micro-USD; otherwise the
  event is a non-monetary ``count`` touch.
* ``identify`` → an :class:`~tally.wire.IdentityLink` joining the hashed ``anonymousId`` to the
  hashed ``userId`` in ``identity_graph``, so later track/span rows stitch to one identity.

Only counts, mapped events and values are persisted — never the raw Segment message body. Every
identifier is HMAC'd under the tenant's key before it touches storage (``messageId`` is the one
non-PII field kept verbatim, as the dedup key).
"""

from __future__ import annotations

from collections.abc import Callable

from tally.wire import BusinessEvent, IdentityLink

from gateway.integration_workers import (
    IngestOutcome,
    IngestWindow,
    IngestWorker,
    as_event_list,
    build_hasher,
    dollars_to_micro,
    iso_to_ns,
)
from gateway.tenant_integration_secrets import IntegrationSecret

DEFAULT_SEGMENT_BASE = "https://api.segment.io"
# Illustrative source-pull endpoint. The real path is a deployment/config detail; the mapping is
# what this ticket pins down and unit-tests.
SEGMENT_EVENTS_PATH = "/v1/events"


def map_track_event(
    event: dict[str, object], hasher: Callable[[str | None], str]
) -> BusinessEvent | None:
    """Map one Segment ``track`` event to a business event, or ``None`` if it isn't a usable track.

    ``messageId`` is Segment's per-message unique id — it becomes ``BusinessEventId`` so redelivery
    collapses under ClickHouse's ReplacingMergeTree. Revenue-bearing track events are monetary;
    everything else is a ``count`` touch (no fabricated value).
    """
    if event.get("type") != "track":
        return None
    message_id = str(event.get("messageId") or "")
    if not message_id:
        return None
    name = str(event.get("event") or "").strip() or "track"
    uid = event.get("userId") or event.get("anonymousId")
    user_hash = hasher(str(uid)) if uid else ""

    props = event.get("properties")
    props = props if isinstance(props, dict) else {}
    revenue = props.get("revenue")
    if revenue is None:
        revenue = props.get("value")
    value_micro = dollars_to_micro(revenue)
    value_type = "monetary" if value_micro is not None else "count"
    currency = str(props.get("currency") or "USD").upper()

    return BusinessEvent(
        business_event_id=message_id,
        event_name=name,
        user_id_hash=user_hash,
        occurred_at_ns=iso_to_ns(event.get("timestamp")),
        value_amount_micro=value_micro,
        value_currency=currency,
        value_type=value_type,
        source="segment",
    )


def map_identify_event(
    event: dict[str, object], hasher: Callable[[str | None], str]
) -> IdentityLink | None:
    """Map one Segment ``identify`` event to an anonymous↔user identity edge, or ``None``.

    Needs BOTH ``anonymousId`` and ``userId`` — an identify with only one side carries no edge to
    record. Both ids are hashed under the tenant key before the link is built.
    """
    if event.get("type") != "identify":
        return None
    user_id = event.get("userId")
    anon_id = event.get("anonymousId")
    if not user_id or not anon_id:
        return None
    anon_hash = hasher(str(anon_id))
    user_hash = hasher(str(user_id))
    if not anon_hash or not user_hash:
        return None
    return IdentityLink(
        identity_a=anon_hash,
        identity_a_type="anonymous_id",
        identity_b=user_hash,
        identity_b_type="user_id",
        observed_at_ns=iso_to_ns(event.get("timestamp")),
        source="segment",
        confidence=1.0,
    )


class SegmentWorker(IngestWorker):
    """Pulls a tenant's Segment events and writes track → events, identify → identity links."""

    connector_id = "segment"

    def _ingest(
        self, tenant_id: str, secret: IntegrationSecret, token: str, window: IngestWindow
    ) -> IngestOutcome:
        base = str(secret.config.get("base_url") or DEFAULT_SEGMENT_BASE).rstrip("/")
        url = base + SEGMENT_EVENTS_PATH
        headers = {"Authorization": f"Bearer {token}"}
        # CTO-219: incremental. Segment's Public API takes ISO-8601 instants; like the endpoint path
        # above, the exact parameter spelling is a deployment detail, and what this ticket pins down
        # is that a cycle asks for a bounded window instead of the provider's default payload.
        payload = self._http.get_json(
            url,
            headers=headers,
            params={"start": window.since_iso(), "end": window.until_iso()},
        )

        hasher = build_hasher(self._registry, tenant_id)
        events: list[BusinessEvent] = []
        links: list[IdentityLink] = []
        errors = 0
        for item in as_event_list(payload):
            if not isinstance(item, dict):
                errors += 1
                continue
            try:
                kind = item.get("type")
                if kind == "track":
                    mapped = map_track_event(item, hasher)
                    if mapped is not None:
                        events.append(mapped)
                elif kind == "identify":
                    link = map_identify_event(item, hasher)
                    if link is not None:
                        links.append(link)
                # page / screen / group / alias → intentionally ignored (not value or identity)
            except Exception:  # noqa: BLE001 — one bad message shouldn't fail the whole cycle
                errors += 1

        inserted = self._insert_events(tenant_id, events)
        inserted += self._insert_links(tenant_id, links)
        error_message = f"{errors} event(s) failed to map" if errors else None
        return IngestOutcome(
            event_count=inserted, partial=errors > 0, error_message=error_message
        )
