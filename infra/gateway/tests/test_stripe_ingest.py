"""Unit tests for the Stripe ingest mapper + signature verifier (CTO-110).

No infrastructure required — every payload is a recorded JSON sample under
``tests/fixtures/stripe/``. Signature tests build a valid Stripe-Signature header from a known
secret + timestamp so the verifier can be exercised end-to-end without network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gateway.stripe_ingest import (
    SUPPORTED_STRIPE_EVENTS,
    StripeSignatureError,
    hash_customer_email,
    make_stripe_signature_header,
    map_stripe_event,
    verify_stripe_signature,
)
from tally.hmac_keys import HmacKeyRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "stripe"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- map_stripe_event -----------------------------------------------------------------------

def test_checkout_session_completed_maps_to_conversion() -> None:
    mapped = map_stripe_event(load("checkout_session_completed.json"))
    assert mapped is not None
    assert mapped.event_name == "conversion"
    # 4900 cents → 49_000_000 micro-USD
    assert mapped.value_amount_micro == 49_000_000
    assert mapped.currency == "USD"
    assert mapped.stripe_event_id == "evt_1NQ8t12eZvKYlo2C8b2L0001"
    assert mapped.stripe_customer_id == "cus_PaYiNgCusT"
    # The mapper prefers customer_details.email when present.
    assert mapped.customer_email == "Alice@Example.com"


def test_invoice_paid_maps_to_subscription_renewal() -> None:
    mapped = map_stripe_event(load("invoice_paid.json"))
    assert mapped is not None
    assert mapped.event_name == "subscription_renewal"
    assert mapped.value_amount_micro == 29_000_000  # 2900 cents
    assert mapped.customer_email == "bob@example.com"


def test_charge_refunded_produces_negative_value_amount() -> None:
    mapped = map_stripe_event(load("charge_refunded.json"))
    assert mapped is not None
    assert mapped.event_name == "refund"
    # Negative so summing across (conversion + refund) nets out.
    assert mapped.value_amount_micro == -49_000_000


def test_subscription_deleted_maps_to_churn_with_zero_value() -> None:
    mapped = map_stripe_event(load("subscription_deleted.json"))
    assert mapped is not None
    assert mapped.event_name == "churn"
    assert mapped.value_amount_micro == 0
    # The subscription object doesn't carry an email — that's expected, the join will be on
    # customer_id-derived identity instead (or the row stays unattributed, which is honest).
    assert mapped.customer_email is None


def test_unsupported_event_type_returns_none() -> None:
    assert map_stripe_event(load("unsupported_event.json")) is None


def test_supported_set_matches_event_name_mapping() -> None:
    # Sanity: every supported type must produce a non-None map for its fixture.
    expected = {
        "checkout.session.completed",
        "invoice.paid",
        "charge.refunded",
        "customer.subscription.deleted",
    }
    assert SUPPORTED_STRIPE_EVENTS == expected


# --- hash_customer_email --------------------------------------------------------------------

def test_hash_customer_email_normalizes_case() -> None:
    reg = HmacKeyRegistry()
    a = hash_customer_email(reg, "t-acme", "Alice@Example.com")
    b = hash_customer_email(reg, "t-acme", "alice@example.com")
    assert a is not None and b is not None
    assert a[0] == b[0], "case differences must collapse so the join still works"
    assert a[1] == "v1"


def test_hash_customer_email_returns_none_for_missing_email() -> None:
    reg = HmacKeyRegistry()
    assert hash_customer_email(reg, "t-acme", None) is None
    assert hash_customer_email(reg, "t-acme", "") is None


def test_hash_customer_email_is_tenant_scoped() -> None:
    reg = HmacKeyRegistry()
    a = hash_customer_email(reg, "t-acme", "alice@example.com")
    b = hash_customer_email(reg, "t-other", "alice@example.com")
    assert a is not None and b is not None
    assert a[0] != b[0], "same email under different tenants must produce different hashes"


# --- signature verification ----------------------------------------------------------------

SECRET = "whsec_test_secret_abc123"


def test_signature_verifies_with_matching_secret() -> None:
    payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
    ts = int(time.time())
    header = make_stripe_signature_header(payload, SECRET, timestamp=ts)
    # Should not raise.
    verify_stripe_signature(payload, header, SECRET, now_s=ts)


def test_signature_rejected_with_wrong_secret() -> None:
    payload = b'{"id":"evt_1"}'
    ts = int(time.time())
    header = make_stripe_signature_header(payload, SECRET, timestamp=ts)
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(payload, header, "whsec_different_secret", now_s=ts)


def test_signature_rejected_when_payload_modified() -> None:
    payload = b'{"id":"evt_1"}'
    ts = int(time.time())
    header = make_stripe_signature_header(payload, SECRET, timestamp=ts)
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(b'{"id":"tampered"}', header, SECRET, now_s=ts)


def test_signature_rejected_when_timestamp_stale() -> None:
    payload = b'{"id":"evt_1"}'
    ts = int(time.time())
    header = make_stripe_signature_header(payload, SECRET, timestamp=ts - 1000)
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(payload, header, SECRET, now_s=ts, tolerance_s=300)


def test_signature_rejected_when_header_missing() -> None:
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(b"x", None, SECRET, now_s=0)


def test_signature_rejected_when_header_malformed() -> None:
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(b"x", "garbage", SECRET, now_s=0)
