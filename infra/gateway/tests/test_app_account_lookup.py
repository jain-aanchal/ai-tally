"""POST /v1/tenant/account-lookup: plaintext account id to AccountIdHash (CTO-185).

The load-bearing test here is :func:`test_hash_matches_sdk_derivation`: the endpoint's digest must
equal the one the SDK's own ``tally.hmac_keys`` path produces for the same tenant and account id,
or the search box returns a well-formed hash that matches no span. The rest of the file guards the
privacy invariant (the plaintext reaches no log line and no stored row) and the two tenant-identifier
spellings the gateway has to live with.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.account_lookup import (
    AccountLookupError,
    MAX_ACCOUNT_ID_BYTES,
    normalize_account_id,
)
from gateway.app import app
from gateway.tenant_identity import FORM_NAME, FORM_UUID, TenantKey, key_form
from tally.hmac_keys import HmacKeyRegistry

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
ACCOUNT_ID = "acme-corp"


class FakeIdentityResolver:
    """Stands in for :class:`TenantIdentityResolver` so no Postgres is needed.

    Mirrors the real contract: caller's spelling first, the other one second when known.
    """

    def __init__(self, pairs: dict[str, str] | None = None) -> None:
        # name -> uuid, both directions resolvable
        self._pairs = pairs if pairs is not None else {TENANT_NAME: TENANT_UUID}

    def key_forms(self, tenant_id: str) -> tuple[TenantKey, ...]:
        given = TenantKey(value=tenant_id, form=key_form(tenant_id))
        for name, uuid_value in self._pairs.items():
            if tenant_id == name:
                return (given, TenantKey(value=uuid_value, form=FORM_UUID))
            if tenant_id == uuid_value:
                return (given, TenantKey(value=name, form=FORM_NAME))
        return (given,)


class OfflineIdentityResolver:
    """Postgres unreachable: the resolver degrades to the caller's spelling and never raises."""

    def key_forms(self, tenant_id: str) -> tuple[TenantKey, ...]:
        return (TenantKey(value=tenant_id, form=key_form(tenant_id)),)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_identity = FakeIdentityResolver()
        app.state.hmac_registry = HmacKeyRegistry()
        yield c


def _lookup(client: TestClient, account_id: object, tenant: str = TENANT_NAME):
    return client.post(
        "/v1/tenant/account-lookup",
        headers={"X-Tenant-Id": tenant},
        json={"account_id": account_id},
    )


# --- SDK parity: the acceptance criterion ------------------------------------------------------


def test_hash_matches_sdk_derivation(client: TestClient) -> None:
    """The endpoint and the SDK's own hmac_keys path must agree, digit for digit.

    Both paths are exercised here: the HTTP endpoint on one side, and on the other a bare
    HmacKeyRegistry driven exactly as an emitting SDK would drive it (provision the tenant, hash
    under the active key version). If these ever diverge, every hash the search box returns stops
    matching the spans the SDK stamped.
    """
    r = _lookup(client, ACCOUNT_ID)
    assert r.status_code == 200
    body = r.json()

    sdk_registry = HmacKeyRegistry()
    sdk_registry.provision(TENANT_NAME)
    expected = sdk_registry.hash(TENANT_NAME, ACCOUNT_ID)

    assert body["account_id_hash"] == expected.value
    assert body["key_version"] == expected.key_version
    # AccountIdHash is FixedString(64) hex in ClickHouse (CTO-180), so the digest must fit it.
    assert len(body["account_id_hash"]) == 64
    assert int(body["account_id_hash"], 16) >= 0


def test_hash_matches_sdk_derivation_for_uuid_spelling(client: TestClient) -> None:
    """Parity holds for a UUID-spelled tenant too, which is what API-key auth resolves to."""
    r = _lookup(client, ACCOUNT_ID, tenant=TENANT_UUID)
    assert r.status_code == 200

    sdk_registry = HmacKeyRegistry()
    sdk_registry.provision(TENANT_UUID)
    expected = sdk_registry.hash(TENANT_UUID, ACCOUNT_ID)
    assert r.json()["account_id_hash"] == expected.value


def test_hash_is_stable_and_account_specific(client: TestClient) -> None:
    first = _lookup(client, ACCOUNT_ID).json()["account_id_hash"]
    second = _lookup(client, ACCOUNT_ID).json()["account_id_hash"]
    other = _lookup(client, "globex-inc").json()["account_id_hash"]
    assert first == second
    assert first != other


def test_hash_is_not_joinable_across_tenants(client: TestClient) -> None:
    """Per-tenant keys: the same account id under two tenants must not collide."""
    app.state.tenant_identity = OfflineIdentityResolver()
    mine = _lookup(client, ACCOUNT_ID, tenant="tenant-a").json()["account_id_hash"]
    theirs = _lookup(client, ACCOUNT_ID, tenant="tenant-b").json()["account_id_hash"]
    assert mine != theirs


# --- unknown accounts are not errors -----------------------------------------------------------


def test_unknown_account_id_returns_a_hash_not_an_error(client: TestClient) -> None:
    """An id nobody ever emitted still gets a valid hash. The tab renders "no spend recorded".

    Answering 404 would leak whether the tenant has this account, and would make a typo
    indistinguishable from a real customer who happens to have no spend in the window.
    """
    r = _lookup(client, "no-such-customer-9f3a")
    assert r.status_code == 200
    body = r.json()
    assert len(body["account_id_hash"]) == 64
    # Nothing about the response claims a match; matching is the caller's ClickHouse query.
    assert "matched" not in body
    assert body["account_id_hash"] != _lookup(client, ACCOUNT_ID).json()["account_id_hash"]


# --- the plaintext never escapes ---------------------------------------------------------------


def test_plaintext_never_appears_in_any_log_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "acme-corp-super-distinctive-7Q4Z"
    with caplog.at_level(logging.DEBUG):
        assert _lookup(client, secret).status_code == 200
    joined = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(caplog.messages)
    assert secret not in joined
    assert "acme-corp-super" not in joined


def test_plaintext_never_appears_in_the_response(client: TestClient) -> None:
    secret = "acme-corp-super-distinctive-7Q4Z"
    r = _lookup(client, secret)
    assert secret not in r.text


def test_rejection_messages_never_echo_the_input(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected lookup is the sneakiest leak path: 422 detail strings get logged by callers."""
    oversized = "x" * 40 + "@leaky-customer.example"
    with caplog.at_level(logging.DEBUG):
        r = client.post(
            "/v1/tenant/account-lookup",
            headers={"X-Tenant-Id": TENANT_NAME},
            json={"account_id": {"nested": oversized}},
        )
    assert r.status_code == 422
    assert "leaky-customer" not in r.text
    assert "leaky-customer" not in "\n".join(caplog.messages)


def test_malformed_json_body_is_rejected_without_quoting_it(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/account-lookup",
        headers={"X-Tenant-Id": TENANT_NAME, "Content-Type": "application/json"},
        content=b'{"account_id": "leaky-customer-value"',
    )
    assert r.status_code == 422
    assert "leaky-customer" not in r.text


def test_lookup_writes_no_row_anywhere(client: TestClient) -> None:
    """The endpoint touches no store. A persisted plaintext would rebuild the reverse map."""

    class ExplodingStore:
        def close(self) -> None:
            """Allowed: lifespan shutdown closes the store after the test body has run."""

        def __getattr__(self, name: str):
            raise AssertionError(f"account lookup must not touch storage (called {name})")

    app.state.store = ExplodingStore()
    app.state.tenant_connectors = ExplodingStore()
    app.state.tenant_feature_value_events = ExplodingStore()
    assert _lookup(client, ACCOUNT_ID).status_code == 200


# --- tenant spellings --------------------------------------------------------------------------


def test_both_tenant_spellings_are_returned(client: TestClient) -> None:
    """Name and UUID derive different keys, so the caller gets both and matches on the set."""
    by_name = _lookup(client, ACCOUNT_ID, tenant=TENANT_NAME).json()
    by_uuid = _lookup(client, ACCOUNT_ID, tenant=TENANT_UUID).json()

    assert [h["tenant_key_form"] for h in by_name["hashes"]] == [FORM_NAME, FORM_UUID]
    assert [h["tenant_key_form"] for h in by_uuid["hashes"]] == [FORM_UUID, FORM_NAME]
    # The caller's own spelling leads, and the two callers cover the same pair of digests.
    assert by_name["account_id_hash"] == by_name["hashes"][0]["account_id_hash"]
    assert {h["account_id_hash"] for h in by_name["hashes"]} == {
        h["account_id_hash"] for h in by_uuid["hashes"]
    }


def test_unresolvable_tenant_still_answers_with_one_hash(client: TestClient) -> None:
    """Postgres down or tenant absent: degrade to the caller's spelling, never fail the lookup."""
    app.state.tenant_identity = OfflineIdentityResolver()
    body = _lookup(client, ACCOUNT_ID).json()
    assert len(body["hashes"]) == 1
    assert body["hashes"][0]["tenant_key_form"] == FORM_NAME

    sdk_registry = HmacKeyRegistry()
    sdk_registry.provision(TENANT_NAME)
    assert body["account_id_hash"] == sdk_registry.hash(TENANT_NAME, ACCOUNT_ID).value


# --- input validation and tenant resolution ----------------------------------------------------


def test_tenant_header_is_required(client: TestClient) -> None:
    r = client.post("/v1/tenant/account-lookup", json={"account_id": ACCOUNT_ID})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [None, "", "   ", 17, ["acme"], {"a": 1}])
def test_invalid_account_ids_are_422(client: TestClient, bad: object) -> None:
    assert _lookup(client, bad).status_code == 422


def test_oversized_account_id_is_422(client: TestClient) -> None:
    assert _lookup(client, "z" * (MAX_ACCOUNT_ID_BYTES + 1)).status_code == 422


def test_body_must_be_an_object(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/account-lookup", headers={"X-Tenant-Id": TENANT_NAME}, json=["acme-corp"]
    )
    assert r.status_code == 422


# --- normalize_account_id, directly ------------------------------------------------------------


def test_normalize_trims_but_does_not_fold_case() -> None:
    """Case matters: an account id is opaque, and `Acme` may be a different customer from `acme`."""
    assert normalize_account_id("  acme-corp  ") == "acme-corp"
    assert normalize_account_id("Acme-Corp") == "Acme-Corp"
    assert normalize_account_id("Acme-Corp") != normalize_account_id("acme-corp")


def test_normalize_error_messages_carry_no_input() -> None:
    oversized = "y" * (MAX_ACCOUNT_ID_BYTES + 1)
    with pytest.raises(AccountLookupError) as exc:
        normalize_account_id(oversized)
    assert oversized not in str(exc.value)
    assert "yyyy" not in str(exc.value)

    for blank in ("", "   "):
        with pytest.raises(AccountLookupError):
            normalize_account_id(blank)
    with pytest.raises(AccountLookupError):
        normalize_account_id(None)
