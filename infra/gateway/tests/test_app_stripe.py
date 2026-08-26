"""App-level tests for POST /v1/stripe/webhook (CTO-110).

Verification path: build a real Stripe-Signature header against a known secret, fake the Postgres
store so it returns that secret, fake the ClickHouse store so we can assert exactly what got
inserted. No infrastructure needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from tally.account_identity import AccountLinker, hash_account_id
from tally.hmac_keys import HmacKeyRegistry

from gateway.app import app
from gateway.stripe_ingest import make_stripe_signature_header
from gateway.tenant_stripe import StripeConfig

FIXTURES = Path(__file__).parent / "fixtures" / "stripe"
SECRET = "whsec_test_secret_xyz"
TENANT = "t-acme"


class FakeCHStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, list]] = []

    def insert_spans(self, rows: list[tuple]) -> int:
        return 0

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        self.events.append((tenant_id, list(events)))
        return len(events)

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeStripeStore:
    def __init__(self, secret: str | None = SECRET) -> None:
        self._secret = secret

    def get(self, tenant_id: str) -> StripeConfig | None:
        if self._secret is None or tenant_id != TENANT:
            return None
        return StripeConfig(
            tenant_id=tenant_id,
            webhook_secret=self._secret,
            stripe_account_id="acct_test",
            connected_at="2026-01-01T00:00:00+00:00",
            disconnected_at=None,
        )


@contextmanager
def _client(secret: str | None = SECRET) -> Iterator[tuple[TestClient, FakeCHStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        ch = FakeCHStore()
        app.state.store = ch
        app.state.tenant_stripe = FakeStripeStore(secret=secret)
        # Reset the in-process dedup set between tests.
        app.state.stripe_event_seen = set()
        # Reset the in-process user→account map between tests (CTO-195).
        app.state.account_linker = AccountLinker()
        yield client, ch


def _post(client: TestClient, payload: bytes, *, secret: str = SECRET, ts: int | None = None):
    timestamp = ts if ts is not None else int(time.time())
    header = make_stripe_signature_header(payload, secret, timestamp=timestamp)
    return client.post(
        f"/v1/stripe/webhook?tenant={TENANT}",
        content=payload,
        headers={"Stripe-Signature": header, "content-type": "application/json"},
    )


def _fixture_bytes(name: str) -> bytes:
    # Round-trip through json.dumps so timestamp manipulation can be done downstream cleanly.
    return json.dumps(json.loads((FIXTURES / name).read_text())).encode("utf-8")


def test_happy_path_inserts_business_event() -> None:
    with _client() as (client, ch):
        body = _fixture_bytes("checkout_session_completed.json")
        # The fixture's `created` is well in the past — bump current time forward via the same
        # timestamp used for signing so the verifier's tolerance window is satisfied.
        r = _post(client, body)
        assert r.status_code == 200, r.text
        assert r.json()["event_name"] == "conversion"
        assert r.json()["value_amount_micro"] == 49_000_000
        assert len(ch.events) == 1
        tenant_id, evs = ch.events[0]
        assert tenant_id == TENANT
        assert len(evs) == 1
        ev = evs[0]
        assert ev.business_event_id == "evt_1NQ8t12eZvKYlo2C8b2L0001"
        assert ev.event_name == "conversion"
        assert ev.source == "stripe"
        assert ev.value_amount_micro == 49_000_000
        assert ev.user_id_hash, "email should have been HMAC'd to populate user_id_hash"


def test_idempotent_on_redelivery() -> None:
    with _client() as (client, ch):
        body = _fixture_bytes("checkout_session_completed.json")
        r1 = _post(client, body)
        r2 = _post(client, body)
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second response signals dedup explicitly so an operator looking at logs can tell.
        assert r2.json().get("deduplicated") is True
        # Exactly one CH insert overall, even though Stripe redelivered.
        assert len(ch.events) == 1


def test_bad_signature_returns_400_and_no_insert() -> None:
    with _client() as (client, ch):
        body = _fixture_bytes("checkout_session_completed.json")
        # Sign with a different secret than the store knows about.
        r = client.post(
            f"/v1/stripe/webhook?tenant={TENANT}",
            content=body,
            headers={
                "Stripe-Signature": make_stripe_signature_header(
                    body, "whsec_wrong_secret", timestamp=int(time.time())
                ),
                "content-type": "application/json",
            },
        )
        assert r.status_code == 400
        assert ch.events == []


def test_unsupported_event_is_acked_but_not_inserted() -> None:
    with _client() as (client, ch):
        body = _fixture_bytes("unsupported_event.json")
        r = _post(client, body)
        assert r.status_code == 200
        assert r.json().get("skipped") is True
        assert ch.events == []


def test_refund_inserts_negative_value() -> None:
    with _client() as (client, ch):
        r = _post(client, _fixture_bytes("charge_refunded.json"))
        assert r.status_code == 200
        ev = ch.events[0][1][0]
        assert ev.event_name == "refund"
        assert ev.value_amount_micro == -49_000_000
        assert ev.value_type == "refund"


def test_subscription_deleted_inserts_churn_with_zero_value() -> None:
    with _client() as (client, ch):
        r = _post(client, _fixture_bytes("subscription_deleted.json"))
        assert r.status_code == 200
        ev = ch.events[0][1][0]
        assert ev.event_name == "churn"
        assert ev.value_amount_micro == 0
        # No email on subscription objects, so the join key is empty — honest, not fabricated.
        assert ev.user_id_hash == ""


# --- account identity (CTO-195) -----------------------------------------------------------------


def _expected_account_hash(customer_id: str) -> str:
    """The hash the route should produce for a Stripe customer id, computed independently."""
    registry = HmacKeyRegistry()
    stamped = hash_account_id(registry, TENANT, customer_id)
    assert stamped is not None
    return stamped.value


def test_stripe_customer_lands_in_account_id_hash() -> None:
    """The Stripe Customer is the account. It must reach AccountIdHash, not UserIdHash."""
    with _client() as (client, ch):
        r = _post(client, _fixture_bytes("checkout_session_completed.json"))
        assert r.status_code == 200
        assert r.json()["account_attributed"] is True
        assert r.json()["account_inferred"] is False
        ev = ch.events[0][1][0]
        assert ev.account_id_hash == _expected_account_hash("cus_PaYiNgCusT")
        assert len(ev.account_id_hash) == 64


def test_account_hash_is_not_the_user_hash() -> None:
    """The whole finding: customer id and end-user email are different identities."""
    with _client() as (client, ch):
        _post(client, _fixture_bytes("checkout_session_completed.json"))
        ev = ch.events[0][1][0]
        assert ev.user_id_hash, "email still populates UserIdHash exactly as before"
        assert ev.account_id_hash != ev.user_id_hash


def test_churn_event_with_no_email_still_gets_an_account() -> None:
    """A subscription object carries no email but does carry the customer, so the account lands."""
    with _client() as (client, ch):
        _post(client, _fixture_bytes("subscription_deleted.json"))
        ev = ch.events[0][1][0]
        assert ev.user_id_hash == ""  # unchanged, honest unattributed user
        assert ev.account_id_hash == _expected_account_hash("cus_PaYiNgCusT")


def test_refund_carries_the_same_account_as_the_charge() -> None:
    """Refunds must net off against the account they were charged to, so they need its hash."""
    with _client() as (client, ch):
        _post(client, _fixture_bytes("charge_refunded.json"))
        ev = ch.events[0][1][0]
        assert ev.value_amount_micro < 0
        assert ev.account_id_hash == _expected_account_hash("cus_PaYiNgCusT")


def test_second_account_for_one_user_raises_a_conflict_but_keeps_the_stated_account() -> None:
    """One user, one account. A stated account is still honoured; the *inference* is withheld.

    The fixtures share ``alice@example.com`` across the checkout and the refund, so pointing the
    refund at a second customer is exactly the multi-account case the plan rules out.
    """
    with _client() as (client, ch):
        _post(client, _fixture_bytes("checkout_session_completed.json"))

        second = json.loads((FIXTURES / "charge_refunded.json").read_text())
        second["data"]["object"]["customer"] = "cus_SomeoneElse"
        _post(client, json.dumps(second).encode("utf-8"))

        linker: AccountLinker = app.state.account_linker
        user_hash = ch.events[0][1][0].user_id_hash
        assert linker.is_ambiguous(TENANT, user_hash) is True
        assert linker.account_for(TENANT, user_hash) == ""
        assert len(linker.conflicts(TENANT)) == 1
        # Stripe named the customer on each event, so each event keeps its own stated account.
        assert ch.events[1][1][0].account_id_hash == _expected_account_hash("cus_SomeoneElse")


def test_account_is_inferred_when_stripe_names_no_customer() -> None:
    """No customer on the payload: fall back to the account this user is already known to be in."""
    with _client() as (client, ch):
        _post(client, _fixture_bytes("checkout_session_completed.json"))

        second = json.loads((FIXTURES / "charge_refunded.json").read_text())
        second["data"]["object"].pop("customer")
        second["id"] = "evt_no_customer"
        r = _post(client, json.dumps(second).encode("utf-8"))

        assert r.json()["account_inferred"] is True
        assert ch.events[1][1][0].account_id_hash == _expected_account_hash("cus_PaYiNgCusT")


def test_nothing_is_inferred_for_an_ambiguous_user() -> None:
    """After a conflict, an event with no stated customer attributes nothing rather than guessing."""
    with _client() as (client, ch):
        _post(client, _fixture_bytes("checkout_session_completed.json"))

        conflicting = json.loads((FIXTURES / "charge_refunded.json").read_text())
        conflicting["data"]["object"]["customer"] = "cus_SomeoneElse"
        _post(client, json.dumps(conflicting).encode("utf-8"))

        orphan = json.loads((FIXTURES / "charge_refunded.json").read_text())
        orphan["data"]["object"].pop("customer")
        orphan["id"] = "evt_orphan"
        r = _post(client, json.dumps(orphan).encode("utf-8"))

        assert r.json()["account_attributed"] is False
        assert ch.events[2][1][0].account_id_hash == ""


def test_missing_tenant_returns_422() -> None:
    with _client() as (client, _ch):
        r = client.post("/v1/stripe/webhook", content=b"{}")
        assert r.status_code == 422


def test_unconnected_tenant_returns_401() -> None:
    # Store returns None → tenant has not connected Stripe.
    with _client(secret=None) as (client, ch):
        r = _post(client, _fixture_bytes("invoice_paid.json"))
        assert r.status_code == 401
        assert ch.events == []
