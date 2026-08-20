"""Pendo ingest worker → business_events (feature first-use touches) (CTO-127).

Pendo is a product-analytics tool. This worker pulls a tenant's feature-engagement aggregation over
an injected HTTP client, authenticated with the tenant's integration key (resolved by reference),
and maps each "feature first-used" record to a low-value attribution touch: a ``feature_first_used``
:class:`~tally.wire.BusinessEvent` of ``ValueType='count'`` with **no** monetary value.

"Low-value" here means exactly that — a feature-use is an engagement signal, not revenue, so we
never fabricate a dollar amount. It rides ``business_events`` as a non-monetary ``count`` touch the
attribution engine can weight downstream. The Pendo visitor id (often an email in the wild) is
HMAC'd under the tenant key and only its hash is ever persisted — including inside the
``BusinessEventId``, which stays dedup-stable per (feature, visitor) without leaking the raw id.
"""

from __future__ import annotations

from collections.abc import Callable

from tally.wire import BusinessEvent

from gateway.integration_workers import (
    IngestOutcome,
    IngestWorker,
    as_event_list,
    build_hasher,
    ms_to_ns,
)
from gateway.tenant_integration_secrets import IntegrationSecret

DEFAULT_PENDO_BASE = "https://app.pendo.io"
# Illustrative feature-engagement aggregation endpoint; the mapping is what this ticket pins down.
PENDO_FEATURE_PATH = "/api/v1/aggregation/features/firstuse"


def map_feature_first_use(
    row: dict[str, object], hasher: Callable[[str | None], str]
) -> BusinessEvent | None:
    """Map one Pendo feature first-use record to a low-value ``count`` touch, or ``None``.

    Requires ``visitorId``, ``featureId`` and a first-use timestamp. The visitor id is hashed and
    the hash (not the raw id) goes into both ``UserIdHash`` and the deterministic ``BusinessEventId``
    so redelivery dedups and no raw identifier is persisted.
    """
    visitor = row.get("visitorId") or row.get("visitor_id")
    feature = row.get("featureId") or row.get("feature_id")
    first_time = row.get("firstTime")
    if first_time is None:
        first_time = row.get("first_time")
    if first_time is None:
        first_time = row.get("firstVisit")
    if not visitor or not feature or first_time is None:
        return None

    visitor_hash = hasher(str(visitor))
    if not visitor_hash:
        return None

    return BusinessEvent(
        business_event_id=f"pendo-firstuse-{feature}-{visitor_hash}",
        event_name="feature_first_used",
        user_id_hash=visitor_hash,
        occurred_at_ns=ms_to_ns(first_time),
        # Engagement touch, not revenue — a low-value 'count' signal, never a fabricated amount.
        value_amount_micro=None,
        value_currency="USD",
        value_type="count",
        source="pendo",
    )


class PendoWorker(IngestWorker):
    """Pulls a tenant's Pendo feature first-use aggregation and writes low-value count touches."""

    connector_id = "pendo"

    def _ingest(
        self, tenant_id: str, secret: IntegrationSecret, token: str
    ) -> IngestOutcome:
        base = str(secret.config.get("base_url") or DEFAULT_PENDO_BASE).rstrip("/")
        url = base + PENDO_FEATURE_PATH
        headers = {"x-pendo-integration-key": token}
        payload = self._http.get_json(url, headers=headers)

        hasher = build_hasher(self._registry, tenant_id)
        events: list[BusinessEvent] = []
        errors = 0
        for item in as_event_list(payload):
            if not isinstance(item, dict):
                errors += 1
                continue
            try:
                mapped = map_feature_first_use(item, hasher)
                if mapped is not None:
                    events.append(mapped)
            except Exception:  # noqa: BLE001 — one bad row shouldn't fail the whole cycle
                errors += 1

        inserted = self._insert_events(tenant_id, events)
        error_message = f"{errors} record(s) failed to map" if errors else None
        return IngestOutcome(
            event_count=inserted, partial=errors > 0, error_message=error_message
        )
