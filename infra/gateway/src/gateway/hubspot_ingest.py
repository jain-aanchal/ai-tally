"""HubSpot ingest worker → business_events (deal conversions) (CTO-127).

HubSpot emits deal-stage change events (a workflow / property-change webhook). This worker pulls a
batch of those events for a tenant over an injected HTTP client, authenticated with the tenant's
OAuth token (resolved by reference), and maps the ones that land a deal in **closed-won** to a
``conversion`` :class:`~tally.wire.BusinessEvent` whose value is ``amount × 1_000_000`` micro-USD.

Non-closed-won stage changes are ignored (they aren't a conversion), so a noisy pipeline doesn't
pollute ``business_events``. The deal's associated contact email, when present, is HMAC'd under the
tenant key so the conversion stitches to the same identity as the spans; a missing email yields an
empty ``UserIdHash`` — honest unattributed revenue.
"""

from __future__ import annotations

from collections.abc import Callable

from tally.wire import BusinessEvent

from gateway.integration_workers import (
    IngestOutcome,
    IngestWorker,
    as_event_list,
    build_hasher,
    dollars_to_micro,
    ms_to_ns,
)
from gateway.tenant_integration_secrets import IntegrationSecret

DEFAULT_HUBSPOT_BASE = "https://api.hubapi.com"
# Illustrative deal-events pull endpoint; the mapping is what this ticket pins down.
HUBSPOT_EVENTS_PATH = "/crm/v3/objects/deals/changes"

# HubSpot's internal stage id for a won deal is ``closedwon``; accept a couple of human spellings
# too so a tenant using a label-valued webhook still maps.
_CLOSED_WON = frozenset({"closedwon", "closed won", "closed-won", "closed_won"})


def _is_closed_won(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _CLOSED_WON


def map_deal_stage_event(
    event: dict[str, object], hasher: Callable[[str | None], str]
) -> BusinessEvent | None:
    """Map one HubSpot deal-stage change to a ``conversion`` event, or ``None`` if not closed-won.

    Recognizes a ``dealstage`` property change whose new value is closed-won. ``eventId`` (unique
    per notification) becomes ``BusinessEventId`` so HubSpot's routine redeliveries dedup; when
    absent we fall back to a deal-scoped deterministic id.
    """
    if str(event.get("propertyName") or "").lower() != "dealstage":
        return None
    if not _is_closed_won(event.get("propertyValue")):
        return None

    object_id = event.get("objectId")
    event_id = event.get("eventId")
    if event_id is not None:
        business_event_id = str(event_id)
    elif object_id is not None:
        business_event_id = f"hubspot-deal-{object_id}-closedwon"
    else:
        return None  # nothing stable to dedup on — drop rather than risk duplicates

    props = event.get("properties")
    props = props if isinstance(props, dict) else {}
    amount = props.get("amount")
    if amount is None:
        amount = event.get("amount")
    value_micro = dollars_to_micro(amount)

    email = props.get("email") or event.get("email")
    user_hash = hasher(str(email).strip().lower()) if email else ""

    return BusinessEvent(
        business_event_id=business_event_id,
        event_name="conversion",
        user_id_hash=user_hash,
        occurred_at_ns=ms_to_ns(event.get("occurredAt")),
        value_amount_micro=value_micro,
        value_currency=str(props.get("currency") or "USD").upper(),
        value_type="monetary",
        source="hubspot",
    )


class HubSpotWorker(IngestWorker):
    """Pulls a tenant's HubSpot deal-stage changes and writes closed-won → conversion events."""

    connector_id = "hubspot"

    def _ingest(
        self, tenant_id: str, secret: IntegrationSecret, token: str
    ) -> IngestOutcome:
        base = str(secret.config.get("base_url") or DEFAULT_HUBSPOT_BASE).rstrip("/")
        url = base + HUBSPOT_EVENTS_PATH
        headers = {"Authorization": f"Bearer {token}"}
        payload = self._http.get_json(url, headers=headers)

        hasher = build_hasher(self._registry, tenant_id)
        events: list[BusinessEvent] = []
        errors = 0
        for item in as_event_list(payload):
            if not isinstance(item, dict):
                errors += 1
                continue
            try:
                mapped = map_deal_stage_event(item, hasher)
                if mapped is not None:
                    events.append(mapped)
            except Exception:  # noqa: BLE001 — one bad event shouldn't fail the whole cycle
                errors += 1

        inserted = self._insert_events(tenant_id, events)
        error_message = f"{errors} event(s) failed to map" if errors else None
        return IngestOutcome(
            event_count=inserted, partial=errors > 0, error_message=error_message
        )
