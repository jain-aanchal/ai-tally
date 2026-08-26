# SPDX-License-Identifier: Apache-2.0
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

Also here: :class:`GenericRevenueConnector` (CTO-199), the documented shape a tenant on a biller we
have no connector for posts directly. It is a fifth entry in the same registry rather than a
parallel pipeline, so revenue arriving that way dedupes and normalizes exactly like the rest.

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

#: ``business_events.ValueType``, the ClickHouse enum in db/clickhouse/attribution.sql, which is
#: what the attribution reader discriminates revenue on (CTO-194). ``monetary`` and ``mrr`` carry
#: money, ``refund`` nets off, ``count`` is an engagement signal with no amount.
VALUE_TYPES = ("monetary", "count", "mrr", "refund")

#: ``Source`` stamped on events arriving through the generic revenue API (CTO-199).
GENERIC_REVENUE_SOURCE = "revenue-api"


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

    ``account_id`` is the tenant's own paying customer (a subscription, a
    contract), raw here and HMAC-hashed into ``business_events.AccountIdHash``
    at the point of storage (CTO-180). ``value_type`` names the
    ``business_events.ValueType`` enum member the event belongs to; ``None``
    means this connector does not classify it, which is deliberately NOT the
    same as claiming it is money.
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
    account_id: str | None = None
    value_type: str | None = None

    def __post_init__(self) -> None:
        if not self.business_event_id:
            raise ValueError("business_event_id must be non-empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(self.value_micro_usd, int) or isinstance(self.value_micro_usd, bool):
            raise ValueError("value_micro_usd must be an int")
        if self.value_micro_usd < 0:
            raise ValueError("value_micro_usd must be non-negative")
        if self.value_type is not None and self.value_type not in VALUE_TYPES:
            raise ValueError(f"value_type must be one of {VALUE_TYPES} or None")

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
            "account_id": self.account_id,
            "value_type": self.value_type,
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
# Generic revenue API (CTO-199)
# --------------------------------------------------------------------------- #
class RevenuePayloadError(ValueError):
    """A generic-revenue payload a caller must fix. Surfaces as HTTP 422."""


#: Currencies are stored as submitted and never converted. See the note on
#: :class:`GenericRevenueConnector`.
_CURRENCY_LEN = 3


def _require_str(payload: Mapping[str, object], key: str, *, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RevenuePayloadError(f"{key} is required and must be a non-empty string")
    text = value.strip()
    if len(text) > max_len:
        raise RevenuePayloadError(f"{key} must be at most {max_len} characters")
    return text


class GenericRevenueConnector:
    """The documented revenue contract for billers we have no connector for (CTO-199).

    Chargebee, Recurly, Zuora and home-grown billing all know how to POST JSON, so the
    alternative to a per-biller connector is one stable, documented shape:

    ``{event_id, account_id, amount, currency, occurred_at, event_name}``

    plus the optional ``value_type`` (defaults to ``monetary``), ``user_id`` and
    ``properties``. Everything downstream sees an ordinary ``business_events`` row, so this is a
    normalization front door and not a second revenue pipeline: the same ``ValueType``
    discrimination and the same per-tenant source narrowing (CTO-194) decide whether it counts.

    Two deliberate strictnesses, both because this is a public contract rather than a webhook we
    receive on someone else's terms:

    * **``event_id`` is required.** It is the caller's idempotency key and the row's
      ``BusinessEventId``. Minting one here would make a retry a second payment.
    * **Bad payloads raise.** :meth:`parse_strict` raises :class:`RevenuePayloadError` with the
      field at fault so the caller gets a 422 they can act on. :meth:`parse` keeps the
      never-raises :class:`CDPConnector` contract for the shared ingest path.

    Currency is recorded, never converted: ``amount`` is in major units of ``currency`` and is
    stored as micro-units of that same currency. We hold no FX rates, and inventing one would put
    a fabricated number in the margin column.
    """

    source = GENERIC_REVENUE_SOURCE

    def parse_strict(self, tenant_id: str, payload: Mapping[str, object]) -> BusinessEvent:
        """Normalize one payload, raising :class:`RevenuePayloadError` on anything unusable."""
        if not isinstance(payload, Mapping):
            raise RevenuePayloadError("body must be a JSON object")
        if not tenant_id:
            raise RevenuePayloadError("tenant_id must be non-empty")

        event_id = _require_str(payload, "event_id", max_len=200)
        account_id = _require_str(payload, "account_id", max_len=512)
        event_name = _require_str(payload, "event_name", max_len=128)

        occurred_at = _parse_iso(payload.get("occurred_at"))
        if occurred_at is None:
            raise RevenuePayloadError(
                "occurred_at is required and must be an ISO-8601 timestamp "
                "(e.g. 2026-08-25T12:00:00Z); a naive timestamp is read as UTC"
            )

        value_type = payload.get("value_type", "monetary")
        if not isinstance(value_type, str) or value_type not in VALUE_TYPES:
            raise RevenuePayloadError(f"value_type must be one of {VALUE_TYPES}")

        currency = payload.get("currency", DEFAULT_CURRENCY)
        if not isinstance(currency, str) or len(currency.strip()) != _CURRENCY_LEN:
            raise RevenuePayloadError("currency must be a 3-letter ISO-4217 code, e.g. USD")
        currency = currency.strip().upper()

        amount = payload.get("amount")
        if value_type == "count":
            # An engagement signal carries no money. Refusing an amount here is what stops a
            # 'count' row from being summed as revenue by a reader that trusts ValueType.
            if amount not in (None, 0):
                raise RevenuePayloadError("a 'count' event must not carry an amount")
            value_micro = 0
        else:
            value_micro = _strict_amount_micro(amount)

        user_id = payload.get("user_id")
        if user_id is not None and not isinstance(user_id, str):
            raise RevenuePayloadError("user_id must be a string when provided")
        properties = payload.get("properties", {})
        if not isinstance(properties, Mapping):
            raise RevenuePayloadError("properties must be a JSON object when provided")

        return BusinessEvent(
            business_event_id=event_id,
            tenant_id=tenant_id,
            source=self.source,
            event_name=event_name,
            occurred_at=occurred_at,
            value_micro_usd=value_micro,
            currency=currency,
            user_id=(user_id.strip() or None) if isinstance(user_id, str) else None,
            properties=dict(properties),
            account_id=account_id,
            value_type=value_type,
        )

    def parse(self, tenant_id: str, payload: Mapping[str, object]) -> ConnectorResult:
        """:class:`CDPConnector` entry point: never raises, empty result on a bad payload."""
        try:
            return ConnectorResult(business_events=(self.parse_strict(tenant_id, payload),))
        except (RevenuePayloadError, ValueError):
            return ConnectorResult()


def _strict_amount_micro(value: object) -> int:
    """``amount`` (major units, number or numeric string) to micro-units. Raises on junk.

    A refund is submitted as a positive amount with ``value_type: "refund"``; the reader is what
    subtracts it. A negative amount is rejected rather than quietly flipped, because guessing
    which of the two a caller meant is how revenue silently goes the wrong way.
    """
    if value is None or isinstance(value, bool):
        raise RevenuePayloadError("amount is required (a number or numeric string in major units)")
    try:
        micro = usd_to_micro(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RevenuePayloadError(f"amount is not a valid decimal: {value!r}") from exc
    if micro < 0:
        raise RevenuePayloadError(
            "amount must be non-negative; submit a refund as a positive amount "
            "with value_type: 'refund'"
        )
    return micro


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

    def forget(self, tenant_id: str, business_event_id: str) -> bool:
        """Un-mark an id. Returns True if it was marked.

        The rollback half of :meth:`mark`. A caller that marks an id and then fails to durably
        store the event must forget it, or the retry it just asked the client to make would be
        swallowed as a duplicate and the revenue lost. Dropping revenue is the same size of bug as
        double counting it, in the opposite direction.
        """
        bucket = self._seen.get(tenant_id)
        if bucket is None or business_event_id not in bucket:
            return False
        bucket.discard(business_event_id)
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
    """A registry wired with the four v1 webhook connectors plus the generic revenue API.

    ``revenue-api`` (CTO-199) is not a provider webhook. It is the documented shape a tenant on
    Chargebee, Recurly, Zuora or a home-grown biller posts directly. It belongs here so it shares
    one deduplicator with the webhook sources: an event id already accepted from a connector
    cannot be re-posted through the API under the same id, and vice versa.
    """
    return ConnectorRegistry(
        (
            SegmentConnector(),
            RudderstackConnector(),
            StripeConnector(),
            HubSpotConnector(),
            GenericRevenueConnector(),
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

    def ingest_revenue_api(self, tenant_id: str, payload: Mapping[str, object]) -> IngestResult:
        """Strict front door for the generic revenue API (CTO-199).

        Identical to :meth:`ingest` on the happy path (same registry, same deduplicator), but a
        payload the connector cannot use raises :class:`RevenuePayloadError` instead of being
        silently skipped. A webhook we did not ask for is best dropped; a documented endpoint a
        customer is integrating against has to say what is wrong with the request.

        Costs one extra parse on the failure path only: the strict re-parse runs solely to recover
        the reason the normal path produced nothing.
        """
        result = self.ingest(GENERIC_REVENUE_SOURCE, tenant_id, payload)
        if result.accepted or result.duplicates:
            return result
        connector = self._registry.get(GENERIC_REVENUE_SOURCE)
        if isinstance(connector, GenericRevenueConnector):
            connector.parse_strict(tenant_id, payload)  # raises RevenuePayloadError
        raise RevenuePayloadError("payload produced no revenue event")

    def forget(self, tenant_id: str, business_event_id: str) -> bool:
        """Roll back a :meth:`ingest` acceptance whose durable write then failed."""
        return self._dedup.forget(tenant_id, business_event_id)
