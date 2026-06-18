"""Stripe webhook ingest → business_events (CTO-110).

Turns the four Stripe events that map cleanly onto value-attribution outcomes (paid checkout,
recurring invoice, refund, subscription cancellation) into rows in the existing ``business_events``
ClickHouse table. The attribution join (cost spans ⋈ revenue events on ``UserIdHash``) lights up
once these rows arrive — see web/lib/clickhouse.ts:queryAttribution.

Idempotency
-----------
Stripe redelivers webhooks routinely (after a 5xx, after operator replay, sometimes just because).
Each Stripe ``event.id`` is unique and durable, so we use it as ``BusinessEventId`` — ClickHouse's
``ReplacingMergeTree`` on ``(TenantId, BusinessEventId)`` will collapse duplicates at merge time,
and the in-process ``IdempotencyCache`` (keyed on ``(tenant_id, event_id)``) blocks the second
insert before it ever reaches CH so the 200 returns fast.

Identity
--------
The email on the Stripe customer is HMAC'd via the same per-tenant key registry used everywhere
else (CTO-74, see tally.hmac_keys) so the resulting ``UserIdHash`` joins with the SDK-emitted
hashes on otel_spans. We hash the lowercased email (Stripe stores it case-preserving but emails are
case-insensitive in practice). Missing-email events get an empty UserIdHash and surface in the
attribution view as unattributed revenue, which is honest.

Webhook secret handling
-----------------------
The raw secret is kept out of logs by going through ``stripe.Webhook.construct_event`` directly —
on a verification failure we surface a structured 400 with no payload echo. The secret never
appears in a log line, and we never persist anything Stripe sent to disk verbatim.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
from dataclasses import dataclass
from typing import Any

from tally.hmac_keys import HmacKeyRegistry

logger = logging.getLogger("tally.gateway.stripe")


# Event types we map. Anything outside this set is ack'd 200 and ignored — Stripe ships hundreds of
# event types and we don't want a noisy 400 if a tenant has unrelated webhooks pointing at us.
SUPPORTED_STRIPE_EVENTS: frozenset[str] = frozenset(
    {
        "checkout.session.completed",
        "invoice.paid",
        "charge.refunded",
        "customer.subscription.deleted",
    }
)

# Map Stripe event_type -> business EventName used by the attribution view.
_EVENT_NAME: dict[str, str] = {
    "checkout.session.completed": "conversion",
    "invoice.paid": "subscription_renewal",
    "charge.refunded": "refund",
    "customer.subscription.deleted": "churn",
}


@dataclass(frozen=True, slots=True)
class StripeEventMapped:
    """A normalized Stripe event ready to become a ``business_events`` row.

    ``value_amount_micro`` is in micro-USD (Stripe ships cents → ×10_000). Refunds are negative,
    churn is ``0``. ``customer_email`` is intentionally retained on this object so the caller can
    HMAC it under the tenant's key — the email never reaches storage.
    """

    event_name: str
    value_amount_micro: int
    stripe_event_id: str
    stripe_customer_id: str | None
    customer_email: str | None
    occurred_at_ns: int
    currency: str


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict by keys, returning ``default`` on any miss / non-dict."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _amount_to_micro_usd(amount_cents: Any) -> int:
    """Cents (Stripe's smallest unit) → integer micro-USD (the codebase invariant)."""
    if amount_cents is None:
        return 0
    try:
        return int(amount_cents) * 10_000
    except (TypeError, ValueError):
        return 0


def map_stripe_event(event: dict[str, Any]) -> StripeEventMapped | None:
    """Map a verified Stripe ``Event`` payload to a :class:`StripeEventMapped`.

    Returns ``None`` for any event type outside :data:`SUPPORTED_STRIPE_EVENTS` so the caller can
    ack-and-skip without polluting ``business_events``.
    """
    event_type = event.get("type")
    if event_type not in SUPPORTED_STRIPE_EVENTS:
        return None
    obj = _get(event, "data", "object", default={}) or {}
    event_id = str(event.get("id") or "")
    if not event_id:
        return None
    # Stripe's ``created`` is unix-seconds. Fall back to ``now`` if absent (shouldn't be).
    created_s = event.get("created")
    if isinstance(created_s, (int, float)):
        occurred_at_ns = int(created_s * 1_000_000_000)
    else:
        import time as _time

        occurred_at_ns = _time.time_ns()

    name = _EVENT_NAME[event_type]
    currency = (
        str(_get(obj, "currency") or _get(obj, "currency_code") or "usd").upper()
    )

    if event_type == "checkout.session.completed":
        value = _amount_to_micro_usd(obj.get("amount_total"))
        customer_id = obj.get("customer")
        email = (
            _get(obj, "customer_details", "email")
            or _get(obj, "customer_email")
        )
    elif event_type == "invoice.paid":
        value = _amount_to_micro_usd(obj.get("amount_paid"))
        customer_id = obj.get("customer")
        email = obj.get("customer_email")
    elif event_type == "charge.refunded":
        # Refund: negative micro-USD so revenue sums net out correctly.
        value = -_amount_to_micro_usd(obj.get("amount_refunded"))
        customer_id = obj.get("customer")
        email = (
            _get(obj, "billing_details", "email")
            or _get(obj, "receipt_email")
        )
    else:  # customer.subscription.deleted
        value = 0
        customer_id = obj.get("customer")
        # Subscription objects don't carry email — the tenant connects via customer_id only.
        email = None

    return StripeEventMapped(
        event_name=name,
        value_amount_micro=value,
        stripe_event_id=event_id,
        stripe_customer_id=str(customer_id) if isinstance(customer_id, str) else None,
        customer_email=str(email) if isinstance(email, str) and email else None,
        occurred_at_ns=occurred_at_ns,
        currency=currency,
    )


def hash_customer_email(
    registry: HmacKeyRegistry,
    tenant_id: str,
    email: str | None,
) -> tuple[str, str] | None:
    """Return ``(user_id_hash, key_version)`` for an email, or ``None`` if no email.

    Lowercases the email before hashing so a customer typing ``Foo@bar.com`` on one path and
    ``foo@bar.com`` on another lands on the same hash and joins to the same span.
    """
    if not email:
        return None
    try:
        registry.provision(tenant_id)
    except ValueError:
        # Empty tenant id — caller should have caught this; treat as un-hashable.
        return None
    stamped = registry.hash(tenant_id, email.strip().lower())
    return stamped.value, stamped.key_version


# --- signature verification (Stripe's scheme, no SDK dependency) -------------------------------
#
# We could ``import stripe`` and call ``stripe.Webhook.construct_event`` — but that pulls a large
# SDK whose only use-case here is one HMAC compare. Stripe's signing scheme is documented:
# https://stripe.com/docs/webhooks#verify-manually. The header looks like::
#
#     Stripe-Signature: t=1492774577,v1=<hex>,v1=<hex>,v0=<hex>
#
# Signed payload is ``"{t}.{raw_body}"`` HMAC-SHA256'd under the webhook secret. We accept the
# payload if any ``v1`` value matches.

# Permitted clock skew between Stripe and us. Stripe defaults to 5 minutes.
DEFAULT_TOLERANCE_S: int = 300


class StripeSignatureError(Exception):
    """Raised when the Stripe-Signature header is missing, malformed, stale, or mismatched."""


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def verify_stripe_signature(
    payload: bytes,
    header: str | None,
    secret: str,
    *,
    now_s: int,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> None:
    """Verify ``Stripe-Signature`` against ``payload`` using ``secret``.

    Raises :class:`StripeSignatureError` on any failure — caller maps that to HTTP 400. Constant-
    time comparison so a wrong secret can't be timing-distinguished from a malformed header.
    """
    if not header:
        raise StripeSignatureError("missing Stripe-Signature header")
    timestamp, signatures = _parse_signature_header(header)
    if timestamp is None or not signatures:
        raise StripeSignatureError("malformed Stripe-Signature header")
    if abs(now_s - timestamp) > tolerance_s:
        raise StripeSignatureError("timestamp outside tolerance")
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = _hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(_hmac.compare_digest(expected, sig) for sig in signatures):
        raise StripeSignatureError("no v1 signature matched")


def make_stripe_signature_header(
    payload: bytes, secret: str, *, timestamp: int
) -> str:
    """Build a ``Stripe-Signature`` header for tests. Mirrors Stripe's documented scheme."""
    signed = f"{timestamp}.".encode("utf-8") + payload
    sig = _hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"
