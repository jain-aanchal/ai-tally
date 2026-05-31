"""CDP / revenue connectors — Segment, Rudderstack, Stripe, HubSpot (CTO-68).

Why this module exists
----------------------
ROI has two halves: the **cost** of an AI feature (the telemetry spine) and the
**value** it produced. The value half lives in CDPs and CRMs — a Segment
``track`` of a conversion, a Stripe ``invoice.paid``, a HubSpot deal moving to
closed-won. This module turns those provider-specific webhook payloads into two
normalized streams the rest of the platform understands:

* :class:`BusinessEvent` — a value event (revenue or a named conversion) written
  to ``business_events``.
* :class:`~tally.identity.IdentifyEvent` / :class:`~tally.identity.AliasEvent` —
  identity links fed into the identity graph (CTO-67) so an anonymous→known
  conversion can reach back to pre-login traces.

Correctness rules baked in
--------------------------
* **occurred_at, not ingest time.** Windowing uses the event's own timestamp.
  Providers are late: Stripe webhooks can trail the real charge by hours. Using
  ingest time would smear revenue into the wrong day and break reconciliation.
* **Idempotent on ``business_event_id``.** Every provider has a stable unique id
  (Segment ``messageId``, Stripe event ``id``, HubSpot ``eventId``). The
  :class:`EventDeduplicator` drops replays so a re-delivered webhook never
  double-counts revenue.
* **Never raises on junk.** A malformed payload yields an empty
  :class:`ConnectorResult` (skipped), not an exception — a bad webhook must not
  take down the ingest path.

Scope: parsing/normalization + dedup only. Stitching (CTO-69) and the ROI UI
(CTO-70) are out of scope. This module is self-contained apart from the
identity event types it reuses from :mod:`tally.identity`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from tally.identity import AliasEvent, IdentifyEvent, IdentityType
from tally.schema import DEFAULT_CURRENCY, usd_to_micro

#: micro-USD per cent — Stripe amounts arrive in the smallest currency unit.
_MICRO_PER_CENT = 10_000


# --------------------------------------------------------------------------- #
# Normalized value event
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BusinessEvent:
    """A normalized value event destined for ``business_events``.

    ``value_micro_usd`` is the revenue attached to the event in integer
    micro-USD (0 for a non-revenue conversion). ``occurred_at`` is the event's
    own timestamp (UTC), never ingest time. ``business_event_id`` is the
    provider's stable id and the idempotency key.
    """

    business_event_id: str
    tenant_id: str
    source: str
    event_name: str
    occurred_at: datetime
    value_micro_usd: int = 0
    currency: str = DEFAULT_CURRENCY
    user_id: str | None = None
    anonymous_id: str | None = None
    properties: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.business_event_id:
            raise ValueError("business_event_id must be non-empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(self.value_micro_usd, int) or isinstance(self.value_micro_usd, bool):
            raise ValueError("value_micro_usd must be an int")
        if self.value_micro_usd < 0:
            raise ValueError("value_micro_usd must be non-negative")

    @property
    def is_revenue(self) -> bool:
        return self.value_micro_usd > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "business_event_id": self.business_event_id,
            "tenant_id": self.tenant_id,
            "source": self.source,
            "event_name": self.event_name,
            "occurred_at": self.occurred_at.isoformat(),
            "value_micro_usd": self.value_micro_usd,
            "currency": self.currency,
            "user_id": self.user_id,
            "anonymous_id": self.anonymous_id,
            "properties": dict(self.properties),
        }


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    """What a connector extracted from one webhook payload.

    A payload may yield a value event, identity links, or both (e.g. a Segment
    ``identify`` with revenue traits). Any of these tuples may be empty.
    """

    business_events: tuple[BusinessEvent, ...] = ()
    identifies: tuple[IdentifyEvent, ...] = ()
    aliases: tuple[AliasEvent, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.business_events or self.identifies or self.aliases)


# --------------------------------------------------------------------------- #
# Parsing helpers (all tolerant — return None on junk)
# --------------------------------------------------------------------------- #
def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (accepts trailing ``Z``). UTC-normalized."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(dt)


def _epoch_to_dt(value: object, *, unit: str) -> datetime | None:
    """Parse a numeric epoch (``unit`` = 's' or 'ms') to a UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / 1000.0 if unit == "ms" else float(value)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _revenue_micro_from_usd(value: object) -> int:
    """Convert a dollar amount (number or numeric string) to micro-USD; 0 on junk."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        micro = usd_to_micro(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    return micro if micro > 0 else 0


def _revenue_micro_from_cents(value: object) -> int:
    """Convert an integer-cents amount to micro-USD; 0 on junk/negative."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    cents = int(value)
    return cents * _MICRO_PER_CENT if cents > 0 else 0


# --------------------------------------------------------------------------- #
# Connector protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class CDPConnector(Protocol):
    """Parses one provider's webhook payload into a :class:`ConnectorResult`."""

    source: str

    def parse(self, tenant_id: str, payload: Mapping[str, object]) -> ConnectorResult: ...


# --------------------------------------------------------------------------- #
# Segment (and Rudderstack, which mirrors the Segment spec)
# --------------------------------------------------------------------------- #
class SegmentConnector:
    """Segment HTTP API / webhook payloads.

    Handles ``track`` (→ value event; ``properties.revenue`` is the amount in
    USD), ``identify`` (→ identity link), and ``alias`` (→ identity link). Other
    types (``page``/``screen``/``group``) produce no events.
    """

    source = "segment"

    def parse(self, tenant_id: str, payload: Mapping[str, object]) -> ConnectorResult:
        if not isinstance(payload, Mapping):
            return ConnectorResult()
        msg_type = _as_str(payload.get("type"))
        occurred_at = _parse_iso(payload.get("timestamp")) or _parse_iso(payload.get("sentAt"))
        event_id = _as_str(payload.get("messageId"))
        user_id = _as_str(payload.get("userId"))
        anon_id = _as_str(payload.get("anonymousId"))

        if msg_type == "track" and event_id and occurred_at is not None:
            props = payload.get("properties")
            props = props if isinstance(props, Mapping) else {}
            value = _revenue_micro_from_usd(props.get("revenue"))
            return ConnectorResult(
                business_events=(
                    BusinessEvent(
                        business_event_id=event_id,
                        tenant_id=tenant_id,
                        source=self.source,
                        event_name=_as_str(payload.get("event")) or "track",
                        occurred_at=occurred_at,
                        value_micro_usd=value,
                        currency=_as_str(props.get("currency")) or DEFAULT_CURRENCY,
                        user_id=user_id,
                        anonymous_id=anon_id,
                        properties=dict(props),
                    ),
                ),
            )

        if msg_type == "identify" and user_id and occurred_at is not None:
            return ConnectorResult(
                identifies=(
                    IdentifyEvent(
                        user_id=user_id,
                        observed_at=occurred_at,
                        anonymous_id=anon_id,
                        source=self.source,
                    ),
                ),
            )

        if msg_type == "alias" and occurred_at is not None:
            prev = _as_str(payload.get("previousId"))
            if prev and user_id:
                return ConnectorResult(
                    aliases=(
                        AliasEvent(
                            previous_id=prev,
                            previous_type=IdentityType.ANONYMOUS_ID,
                            new_id=user_id,
                            new_type=IdentityType.USER_ID,
                            observed_at=occurred_at,
                            source=self.source,
                        ),
                    ),
                )
        return ConnectorResult()


class RudderstackConnector(SegmentConnector):
    """Rudderstack emits the Segment event spec; only the source tag differs."""

    source = "rudderstack"


# --------------------------------------------------------------------------- #
# Stripe
# --------------------------------------------------------------------------- #
class StripeConnector:
    """Stripe ``Event`` webhooks (e.g. ``invoice.paid``, ``charge.succeeded``).

    Amounts arrive in integer **cents**. The event ``id`` is the idempotency
    key; ``created`` (epoch seconds) is the occurred_at. The customer id becomes
    an external user identity so revenue can be attributed.
    """

    source = "stripe"

    #: Event types we treat as revenue, mapped to the amount field on the object.
    _REVENUE_TYPES = {
        "invoice.paid": "amount_paid",
        "invoice.payment_succeeded": "amount_paid",
        "charge.succeeded": "amount",
        "payment_intent.succeeded": "amount",
        "checkout.session.completed": "amount_total",
    }

    def parse(self, tenant_id: str, payload: Mapping[str, object]) -> ConnectorResult:
        if not isinstance(payload, Mapping):
            return ConnectorResult()
        event_id = _as_str(payload.get("id"))
        event_type = _as_str(payload.get("type"))
        occurred_at = _epoch_to_dt(payload.get("created"), unit="s")
        if not event_id or not event_type or occurred_at is None:
            return ConnectorResult()

        data = payload.get("data")
        obj = data.get("object") if isinstance(data, Mapping) else None
        obj = obj if isinstance(obj, Mapping) else {}

        amount_field = self._REVENUE_TYPES.get(event_type)
        value = _revenue_micro_from_cents(obj.get(amount_field)) if amount_field else 0
        customer = _as_str(obj.get("customer"))
        currency = _as_str(obj.get("currency"))

        return ConnectorResult(
            business_events=(
                BusinessEvent(
                    business_event_id=event_id,
                    tenant_id=tenant_id,
                    source=self.source,
                    event_name=event_type,
                    occurred_at=occurred_at,
                    value_micro_usd=value,
                    currency=(currency or DEFAULT_CURRENCY).upper(),
                    user_id=customer,
                    properties={"stripe_object": _as_str(obj.get("object"))},
                ),
            ),
        )


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #
class HubSpotConnector:
    """HubSpot webhook subscription payloads (deal/contact property changes).

    ``eventId`` is the idempotency key; ``occurredAt`` is epoch milliseconds.
    A deal ``amount`` (USD) becomes the value; the object id becomes an external
    user identity.
    """

    source = "hubspot"

    def parse(self, tenant_id: str, payload: Mapping[str, object]) -> ConnectorResult:
        if not isinstance(payload, Mapping):
            return ConnectorResult()
        event_id = _as_str(payload.get("eventId"))
        occurred_at = _epoch_to_dt(payload.get("occurredAt"), unit="ms")
        if not event_id or occurred_at is None:
            return ConnectorResult()

        subscription = _as_str(payload.get("subscriptionType")) or "hubspot.event"
        props = payload.get("properties")
        props = props if isinstance(props, Mapping) else {}
        value = _revenue_micro_from_usd(props.get("amount"))
        object_id = _as_str(payload.get("objectId"))

        return ConnectorResult(
            business_events=(
                BusinessEvent(
                    business_event_id=event_id,
                    tenant_id=tenant_id,
                    source=self.source,
                    event_name=subscription,
                    occurred_at=occurred_at,
                    value_micro_usd=value,
                    user_id=object_id,
                    properties=dict(props),
                ),
            ),
        )


# --------------------------------------------------------------------------- #
# Deduplication (idempotency + replay safety)
# --------------------------------------------------------------------------- #
class EventDeduplicator:
    """Tracks seen ``business_event_id`` s per tenant to drop replays.

    A re-delivered or replayed webhook carries the same provider id; marking it
    once means subsequent deliveries are ignored, so revenue is never
    double-counted regardless of how late or how often it arrives.
    """

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {}

    def is_duplicate(self, tenant_id: str, business_event_id: str) -> bool:
        return business_event_id in self._seen.get(tenant_id, set())

    def mark(self, tenant_id: str, business_event_id: str) -> bool:
        """Record an id. Returns True if newly seen, False if it was a duplicate."""
        bucket = self._seen.setdefault(tenant_id, set())
        if business_event_id in bucket:
            return False
        bucket.add(business_event_id)
        return True

    def count(self, tenant_id: str) -> int:
        return len(self._seen.get(tenant_id, set()))


# --------------------------------------------------------------------------- #
# Registry + ingestor
# --------------------------------------------------------------------------- #
class ConnectorRegistry:
    """Maps a source name to its connector."""

    __slots__ = ("_by_source",)

    def __init__(self, connectors: tuple[CDPConnector, ...] = ()) -> None:
        self._by_source: dict[str, CDPConnector] = {c.source: c for c in connectors}

    def register(self, connector: CDPConnector) -> None:
        self._by_source[connector.source] = connector

    def get(self, source: str) -> CDPConnector | None:
        return self._by_source.get(source)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_source))


def default_registry() -> ConnectorRegistry:
    """A registry wired with all four v1 connectors."""
    return ConnectorRegistry(
        (
            SegmentConnector(),
            RudderstackConnector(),
            StripeConnector(),
            HubSpotConnector(),
        )
    )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of ingesting one webhook.

    ``accepted`` are the newly-seen value events; ``duplicates`` is how many
    value events were dropped as replays. Identity events are passed through
    (the identity graph is itself idempotent on edges).
    """

    accepted: tuple[BusinessEvent, ...]
    duplicates: int
    identifies: tuple[IdentifyEvent, ...]
    aliases: tuple[AliasEvent, ...]

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def total_value_micro_usd(self) -> int:
        return sum(e.value_micro_usd for e in self.accepted)

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_count": self.accepted_count,
            "duplicates": self.duplicates,
            "total_value_micro_usd": self.total_value_micro_usd,
            "identifies": len(self.identifies),
            "aliases": len(self.aliases),
        }


class WebhookIngestor:
    """Routes a webhook to its connector, dedupes value events, returns results.

    Stateless except for the injected :class:`EventDeduplicator`. Unknown
    sources and unparseable payloads yield an empty :class:`IngestResult` rather
    than raising — a bad webhook is skipped, never fatal.
    """

    __slots__ = ("_registry", "_dedup")

    def __init__(
        self,
        registry: ConnectorRegistry | None = None,
        deduplicator: EventDeduplicator | None = None,
    ) -> None:
        self._registry = registry if registry is not None else default_registry()
        self._dedup = deduplicator if deduplicator is not None else EventDeduplicator()

    def ingest(
        self, source: str, tenant_id: str, payload: Mapping[str, object]
    ) -> IngestResult:
        connector = self._registry.get(source)
        if connector is None or not tenant_id:
            return IngestResult((), 0, (), ())
        try:
            result = connector.parse(tenant_id, payload)
        except Exception:
            # Defensive: a connector bug must not break ingest.
            return IngestResult((), 0, (), ())

        accepted: list[BusinessEvent] = []
        duplicates = 0
        for event in result.business_events:
            if self._dedup.mark(tenant_id, event.business_event_id):
                accepted.append(event)
            else:
                duplicates += 1
        return IngestResult(
            accepted=tuple(accepted),
            duplicates=duplicates,
            identifies=result.identifies,
            aliases=result.aliases,
        )
