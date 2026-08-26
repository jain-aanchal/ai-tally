# SPDX-License-Identifier: Apache-2.0
"""Generic revenue API: the storage-side half of CTO-199 (E6).

``POST /v1/revenue/events`` is the documented, automatable alternative to a CSV upload for tenants
billing through Chargebee, Recurly, Zuora or something home-grown. The payload shape and its
validation live in :class:`tally.cdp_connectors.GenericRevenueConnector`, because normalization is
the SDK's job and this endpoint is deliberately not a second revenue pipeline. What lives here is
everything that only makes sense next to storage: hashing the identifiers, mapping onto the
``business_events`` wire row, and answering whether the tenant's own revenue policy will count what
they just posted.

Three things this module is careful about.

**Hashing goes through the per-tenant HMAC path.** ``account_id`` never reaches ClickHouse in the
clear; it is HMAC-SHA256'd under the tenant's key (:mod:`tally.hmac_keys`) exactly like every other
identifier, so an account hash cannot be reversed or correlated across tenants.

**The account id doubles as the user identity when no ``user_id`` is given.** Today's attribution
revenue query joins ``business_events.UserIdHash`` to ``otel_spans.UserIdHash``, and the Stripe
connector already puts the Stripe *customer* id there, a customer being the account in B2B SaaS.
Mirroring that is what makes revenue posted here land in the margin numbers identically to
connector-sourced revenue instead of behaving like a special case. Both columns are filled from the
same deterministic HMAC, so they agree, and E2 (routing connector identity to ``AccountIdHash``)
converges on the same value rather than fighting it.

**The raw payload is not stored.** ``business_events.RawPayload`` stays empty: the payload's whole
distinguishing content is the raw ``account_id``, which is the one thing hashing exists to keep out
of the telemetry store.
"""

from __future__ import annotations

import logging

from tally.cdp_connectors import (
    GENERIC_REVENUE_SOURCE,
    BusinessEvent as NormalizedEvent,
)
from tally.hmac_keys import HmacKeyRegistry
from tally.wire import BusinessEvent as WireBusinessEvent

from gateway.tenant_revenue_sources import RevenueSourceConfig, TenantRevenueSourceStore

logger = logging.getLogger("tally.gateway")

#: ``ValueType`` values the attribution reader sums as positive revenue, mirroring
#: ``POSITIVE_VALUE_TYPES`` in web/lib/revenueSources.ts.
POSITIVE_VALUE_TYPES = ("monetary", "mrr")
REFUND_VALUE_TYPE = "refund"


def to_wire_event(
    registry: HmacKeyRegistry, tenant_id: str, event: NormalizedEvent
) -> WireBusinessEvent:
    """Hash the identifiers and map a normalized event onto the ``business_events`` wire row.

    ``account_id`` is required by the connector, so ``AccountIdHash`` is always populated here:
    an event through this endpoint is never in the unattributed bucket. ``user_id`` is optional and
    falls back to the account id (see the module docstring).
    """
    registry.provision(tenant_id)
    account_hash = registry.hash(tenant_id, event.account_id or "").value
    user_source = event.user_id or event.account_id or ""
    user_hash = registry.hash(tenant_id, user_source).value if user_source else ""

    # A 'count' event carries no money. NULL, not 0: a zero would be a claim that this customer
    # generated no revenue, which is not what an engagement signal says.
    amount = event.value_micro_usd if event.value_type != "count" else None

    return WireBusinessEvent(
        business_event_id=event.business_event_id,
        event_name=event.event_name,
        user_id_hash=user_hash,
        occurred_at_ns=int(event.occurred_at.timestamp() * 1_000_000_000),
        value_amount_micro=amount,
        value_currency=event.currency,
        value_type=event.value_type or "monetary",
        # Lowercased to match the normalization web/lib/revenueSources.ts compares Source against.
        source=GENERIC_REVENUE_SOURCE.lower(),
        account_id_hash=account_hash,
    )


def counts_as_revenue(config: RevenueSourceConfig | None, source: str, value_type: str) -> bool:
    """Would the tenant's revenue policy count this event on /attribution?

    A pure mirror of ``revenueSourceFilter`` + ``positiveValueTypes`` in web/lib/revenueSources.ts,
    evaluated for one event. This is the reason the endpoint is not a bypass: a tenant who has
    narrowed ``revenue_sources`` to ``['stripe']`` and then posts here would otherwise watch their
    revenue land in ClickHouse and never appear on the dashboard, with nothing anywhere saying why.
    The answer is returned on the 200 so the integration surfaces it on the first request.

    ``config is None`` means the tenant has no row, which is the default policy: every source
    counts, monetary and mrr are revenue, refunds net off.
    """
    if value_type == REFUND_VALUE_TYPE:
        counted_type = True
    elif value_type == "mrr":
        counted_type = config is None or config.include_mrr
    else:
        counted_type = value_type in POSITIVE_VALUE_TYPES
    if not counted_type:
        return False
    if config is None or not config.revenue_sources:
        return True
    return source.lower() in tuple(s.lower() for s in config.revenue_sources)


def revenue_policy_note(
    store: TenantRevenueSourceStore, tenant_id: str, source: str, value_type: str
) -> bool | None:
    """``counts_as_revenue`` against the tenant's stored policy, or ``None`` if it can't be read.

    Best-effort by design. The ingest must not fail because the control-plane database blinked, and
    an unknown answer is reported as unknown rather than as a confident ``true``, the same
    honest-under-uncertainty rule the dashboard renders blanks under.
    """
    try:
        config = store.get_for_caller(tenant_id)
    except Exception:  # noqa: BLE001 (a policy read must never fail an accepted revenue event)
        logger.warning("revenue policy lookup failed for tenant %s", tenant_id, exc_info=True)
        return None
    return counts_as_revenue(config, source, value_type)
