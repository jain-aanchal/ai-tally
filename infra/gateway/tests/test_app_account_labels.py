"""GET/POST/DELETE /v1/tenant/account-labels: optional human-readable account names (CTO-186).

Two tests carry the acceptance criteria:

* :func:`test_upsert_on_conflict_renames_in_place` is the upsert-on-conflict path. Setting and
  renaming are the same call, and a rename must replace the label rather than add a second row.
* :func:`test_delete_reverts_account_to_hash` is the escape hatch. Deleting must actually remove
  the row so the tab falls back to the hash, because a tombstone would keep the customer name on
  disk after the tenant asked us to forget it.

The rest guard the invariants around those: a label never reaches ClickHouse, an unlabelled account
is a supported state rather than an error, and the two spellings of the tenant identifier derive two
different account hashes so labelling by plaintext has to cover both.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_account_labels import (
    AccountLabel,
    AccountLabelError,
    MAX_LABEL_CHARS,
    TenantNotFound,
    normalize_account_id_hash,
    normalize_label,
)
from gateway.tenant_identity import FORM_NAME, FORM_UUID, TenantKey, key_form
from tally.hmac_keys import HmacKeyRegistry

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
ACCOUNT_ID = "acme-corp"

HASH_A = "a" * 64
HASH_B = "b" * 64


class FakeIdentityResolver:
    """Stands in for :class:`TenantIdentityResolver` so no Postgres is needed.

    Mirrors the real contract: the caller's spelling first, the other one second when known.
    """

    def key_forms(self, tenant_id: str) -> tuple[TenantKey, ...]:
        given = TenantKey(value=tenant_id, form=key_form(tenant_id))
        if tenant_id == TENANT_NAME:
            return (given, TenantKey(value=TENANT_UUID, form=FORM_UUID))
        if tenant_id == TENANT_UUID:
            return (given, TenantKey(value=TENANT_NAME, form=FORM_NAME))
        return (given,)


class SingleSpellingResolver:
    """Postgres unreachable: the resolver degrades to the caller's spelling and never raises."""

    def key_forms(self, tenant_id: str) -> tuple[TenantKey, ...]:
        return (TenantKey(value=tenant_id, form=key_form(tenant_id)),)


class FakeStore:
    """In-memory stand-in for :class:`TenantAccountLabelStore`.

    Mirrors the real contract that matters to the endpoints: last-write-wins upsert keyed on
    ``(tenant, hash)``, and a delete that genuinely removes the entry. Tenant identifiers are
    folded onto one key the way ``_resolve_tenant_uuid`` folds a name onto ``tenants.id``, so the
    fake shows the same "one tenant, one row" behaviour the foreign key enforces.
    """

    def __init__(self, known_tenants: set[str] | None = None) -> None:
        self._rows: dict[tuple[str, str], AccountLabel] = {}
        self._known = (
            known_tenants if known_tenants is not None else {TENANT_NAME, TENANT_UUID}
        )

    def _resolve(self, tenant_id: str) -> str:
        if tenant_id not in self._known:
            raise TenantNotFound("no tenant matches the supplied identifier")
        return TENANT_UUID if tenant_id in {TENANT_NAME, TENANT_UUID} else tenant_id

    def list(self, tenant_id: str) -> list[AccountLabel]:
        resolved = self._resolve(tenant_id)
        rows = [row for (t, _), row in self._rows.items() if t == resolved]
        return sorted(rows, key=lambda r: (r.label, r.account_id_hash))

    def get(self, tenant_id: str, account_id_hash: str) -> AccountLabel | None:
        resolved = self._resolve(tenant_id)
        return self._rows.get((resolved, normalize_account_id_hash(account_id_hash)))

    def upsert(self, tenant_id: str, account_id_hash: str, *, label: str) -> AccountLabel:
        return self.upsert_many(tenant_id, [account_id_hash], label=label)[0]

    def upsert_many(
        self, tenant_id: str, account_id_hashes: list[str], *, label: str
    ) -> list[AccountLabel]:
        label = normalize_label(label)
        hashes = [normalize_account_id_hash(h) for h in account_id_hashes]
        if not hashes:
            raise AccountLabelError("at least one account_id_hash required")
        resolved = self._resolve(tenant_id)
        out: list[AccountLabel] = []
        for account_id_hash in hashes:
            row = AccountLabel(
                account_id_hash=account_id_hash,
                label=label,
                updated_at=datetime.now(tz=timezone.utc).isoformat(),
            )
            self._rows[(resolved, account_id_hash)] = row
            out.append(row)
        return out

    def delete(self, tenant_id: str, account_id_hash: str) -> bool:
        return self.delete_many(tenant_id, [account_id_hash]) > 0

    def delete_many(self, tenant_id: str, account_id_hashes: list[str]) -> int:
        hashes = [normalize_account_id_hash(h) for h in account_id_hashes]
        if not hashes:
            raise AccountLabelError("at least one account_id_hash required")
        resolved = self._resolve(tenant_id)
        removed = 0
        for account_id_hash in hashes:
            if self._rows.pop((resolved, account_id_hash), None) is not None:
                removed += 1
        return removed

    def record_observed_label(
        self, tenant_id: str, account_id_hash: str, label: object
    ) -> AccountLabel | None:
        if label is None:
            return None
        try:
            return self.upsert(tenant_id, account_id_hash, label=label)
        except AccountLabelError:
            return None


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_account_labels = store
        app.state.tenant_identity = FakeIdentityResolver()
        app.state.hmac_registry = HmacKeyRegistry()
        yield c


def _get(client: TestClient, tenant: str = TENANT_NAME):
    return client.get("/v1/tenant/account-labels", headers={"X-Tenant-Id": tenant})


def _post(client: TestClient, body: dict, tenant: str = TENANT_NAME):
    return client.post(
        "/v1/tenant/account-labels", headers={"X-Tenant-Id": tenant}, json=body
    )


def _delete(client: TestClient, body: dict, tenant: str = TENANT_NAME):
    return client.request(
        "DELETE", "/v1/tenant/account-labels", headers={"X-Tenant-Id": tenant}, json=body
    )


# --- the two acceptance paths ------------------------------------------------------------------


def test_upsert_on_conflict_renames_in_place(client: TestClient) -> None:
    """Setting then renaming touches ONE row, and the second label wins.

    This is the conflict path. If the upsert did not resolve the conflict the second call would
    either error on the primary key or leave two labels for one account, which is precisely the
    "which of two labels wins" ambiguity that keeping labels off the span was meant to avoid.
    """
    first = _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"})
    assert first.status_code == 200, first.text
    assert first.json()["label"]["label"] == "Acme Corp"

    second = _post(client, {"account_id_hash": HASH_A, "label": "Acme Corporation"})
    assert second.status_code == 200, second.text
    assert second.json()["label"]["label"] == "Acme Corporation"

    labels = _get(client).json()["labels"]
    assert len(labels) == 1, "rename must replace the label, not add a second row"
    assert labels[0]["account_id_hash"] == HASH_A
    assert labels[0]["label"] == "Acme Corporation"


def test_upsert_is_idempotent_without_a_change_id(client: TestClient) -> None:
    """Replaying the same body is a no-op. A label is last-write-wins, so no token is needed."""
    body = {"account_id_hash": HASH_A, "label": "Acme Corp"}
    assert _post(client, body).status_code == 200
    assert _post(client, body).status_code == 200
    labels = _get(client).json()["labels"]
    assert len(labels) == 1
    assert labels[0]["label"] == "Acme Corp"


def test_delete_reverts_account_to_hash(client: TestClient, store: FakeStore) -> None:
    """Deleting removes the row outright, so the tab falls back to the hash.

    The escape hatch. A tombstoned or audit-logged label would still be a customer name sitting in
    our storage after the tenant asked us to drop it, so this asserts the row is gone rather than
    merely hidden.
    """
    _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"})
    assert len(_get(client).json()["labels"]) == 1

    removed = _delete(client, {"account_id_hash": HASH_A})
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True

    assert _get(client).json()["labels"] == [], "row must be gone, not tombstoned"
    assert store.get(TENANT_NAME, HASH_A) is None
    # And nothing anywhere still remembers the name.
    assert not any("Acme" in row.label for row in store.list(TENANT_NAME))


def test_delete_of_unlabelled_account_is_not_an_error(client: TestClient) -> None:
    """A double-click is not a 404. The requested end state is the one the caller gets."""
    r = _delete(client, {"account_id_hash": HASH_B})
    assert r.status_code == 200
    assert r.json()["removed"] is False


def test_relabel_after_delete_works(client: TestClient) -> None:
    """Delete is not a permanent refusal: a tenant may change their mind back."""
    _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"})
    _delete(client, {"account_id_hash": HASH_A})
    again = _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"})
    assert again.status_code == 200
    assert _get(client).json()["labels"][0]["label"] == "Acme Corp"


# --- labels are optional -----------------------------------------------------------------------


def test_fresh_tenant_has_no_labels(client: TestClient) -> None:
    """A tenant that sets no labels is a supported state, not missing data."""
    r = _get(client)
    assert r.status_code == 200
    assert r.json() == {"tenant_id": TENANT_NAME, "labels": []}


def test_labels_are_per_account_not_all_or_nothing(client: TestClient) -> None:
    """Labelling one account leaves the other unlabelled and still listable."""
    _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"})
    labels = _get(client).json()["labels"]
    assert [row["account_id_hash"] for row in labels] == [HASH_A]


# --- the two tenant spellings ------------------------------------------------------------------


def test_labelling_by_plaintext_covers_both_tenant_key_spaces(
    client: TestClient, store: FakeStore
) -> None:
    """A label set by plaintext account id attaches under EVERY tenant spelling.

    For HMAC the tenant identifier is key material, so ``local-dev`` and its UUID derive two
    unrelated key spaces and two different digests for the same account. Writing under only one
    would label spans ingested through one door and leave the other showing raw hex.
    """
    r = _post(client, {"account_id": ACCOUNT_ID, "label": "Acme Corp"})
    assert r.status_code == 200, r.text
    written = r.json()["labels"]
    assert len(written) == 2, "one row per tenant spelling"
    assert written[0]["account_id_hash"] != written[1]["account_id_hash"]
    assert all(row["label"] == "Acme Corp" for row in written)

    # And the same account looked up under the OTHER spelling of the tenant resolves to a hash
    # that is already labelled.
    lookup = client.post(
        "/v1/tenant/account-lookup",
        headers={"X-Tenant-Id": TENANT_UUID},
        json={"account_id": ACCOUNT_ID},
    ).json()
    labelled = {row.account_id_hash for row in store.list(TENANT_UUID)}
    assert {h["account_id_hash"] for h in lookup["hashes"]} <= labelled


def test_hashes_written_match_the_lookup_endpoint(client: TestClient) -> None:
    """The store keys on exactly the digests /v1/tenant/account-lookup hands out.

    If these two drifted, an operator could search for an account, get a hash, and find the label
    they just set attached to a different one.
    """
    lookup = client.post(
        "/v1/tenant/account-lookup",
        headers={"X-Tenant-Id": TENANT_NAME},
        json={"account_id": ACCOUNT_ID},
    ).json()
    expected = [h["account_id_hash"] for h in lookup["hashes"]]

    written = _post(client, {"account_id": ACCOUNT_ID, "label": "Acme Corp"}).json()
    assert [row["account_id_hash"] for row in written["labels"]] == expected


def test_delete_by_plaintext_clears_every_key_space(client: TestClient) -> None:
    """Unlabelling by plaintext must clear all spellings, or the escape hatch leaks a name."""
    _post(client, {"account_id": ACCOUNT_ID, "label": "Acme Corp"})
    assert len(_get(client).json()["labels"]) == 2

    r = _delete(client, {"account_id": ACCOUNT_ID})
    assert r.status_code == 200
    assert r.json()["rows_removed"] == 2
    assert _get(client).json()["labels"] == []


def test_single_spelling_tenant_writes_one_row(store: FakeStore) -> None:
    """When Postgres cannot resolve the other spelling we still write the caller's own."""
    with TestClient(app) as c:
        app.state.tenant_account_labels = store
        app.state.tenant_identity = SingleSpellingResolver()
        app.state.hmac_registry = HmacKeyRegistry()
        r = _post(c, {"account_id": ACCOUNT_ID, "label": "Acme Corp"})
        assert r.status_code == 200, r.text
        assert len(r.json()["labels"]) == 1


# --- the label never reaches ClickHouse --------------------------------------------------------


def test_label_write_touches_no_span_store(client: TestClient) -> None:
    """Setting a label performs no ClickHouse write of any kind.

    The central invariant of this ticket. The label is control-plane metadata joined at render
    time; if it ever reached the span store it would put customer names in telemetry, which is what
    AccountIdHash exists to prevent.
    """

    class ExplodingStore:
        def __getattr__(self, name: str):  # pragma: no cover - only fires on regression
            raise AssertionError(f"label path touched the span store: {name}")

    real_store = app.state.store
    app.state.store = ExplodingStore()
    try:
        assert _post(client, {"account_id_hash": HASH_A, "label": "Acme Corp"}).status_code == 200
        assert _delete(client, {"account_id_hash": HASH_A}).status_code == 200
        assert _get(client).status_code == 200
    finally:
        app.state.store = real_store


def test_label_is_not_echoed_in_validation_errors(client: TestClient) -> None:
    """Rejection messages never quote the customer name that was submitted."""
    secret = "Very Secret Customer Name " * 40
    r = _post(client, {"account_id_hash": HASH_A, "label": secret})
    assert r.status_code == 422
    assert "Secret" not in r.text


def test_account_id_is_not_echoed_in_validation_errors(client: TestClient) -> None:
    r = _post(client, {"account_id": "x" * 5000, "label": "Acme Corp"})
    assert r.status_code == 422
    assert "xxxx" not in r.text


# --- request validation ------------------------------------------------------------------------


def test_exactly_one_identifier_required(client: TestClient) -> None:
    both = _post(
        client, {"account_id": ACCOUNT_ID, "account_id_hash": HASH_A, "label": "Acme"}
    )
    assert both.status_code == 422
    neither = _post(client, {"label": "Acme"})
    assert neither.status_code == 422


def test_empty_label_is_rejected_rather_than_treated_as_a_delete(client: TestClient) -> None:
    """An empty string is not a second, weaker deletion that leaves a row behind."""
    r = _post(client, {"account_id_hash": HASH_A, "label": "   "})
    assert r.status_code == 422
    assert _get(client).json()["labels"] == []


def test_non_hex_account_id_hash_is_rejected(client: TestClient) -> None:
    """Catches a plaintext account id pasted into the hash field, which would never match."""
    r = _post(client, {"account_id_hash": "acme-corp-not-a-digest-but-long-enough-yes", "label": "A"})
    assert r.status_code == 422


def test_body_must_be_a_json_object(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/account-labels", headers={"X-Tenant-Id": TENANT_NAME}, json=["nope"]
    )
    assert r.status_code == 422


def test_unknown_tenant_is_404(client: TestClient) -> None:
    r = _post(client, {"account_id_hash": HASH_A, "label": "Acme"}, tenant="no-such-tenant")
    assert r.status_code == 404


# --- pure validators ---------------------------------------------------------------------------


def test_normalize_label_trims_but_does_not_fold_case() -> None:
    assert normalize_label("  Acme Corp  ") == "Acme Corp"
    assert normalize_label("ACME") == "ACME"


def test_normalize_label_rejects_oversized_without_echoing() -> None:
    with pytest.raises(AccountLabelError) as exc:
        normalize_label("z" * (MAX_LABEL_CHARS + 1))
    assert "zzz" not in str(exc.value)


def test_normalize_account_id_hash_lowercases() -> None:
    assert normalize_account_id_hash("A" * 64) == "a" * 64


def test_normalize_account_id_hash_rejects_short_values() -> None:
    with pytest.raises(AccountLabelError):
        normalize_account_id_hash("abc")


# --- the CTO-182 (B3) seam ---------------------------------------------------------------------


def test_record_observed_label_upserts(store: FakeStore) -> None:
    """The seam a span-borne ``gen_ai.account_label`` will call once B3 lands."""
    row = store.record_observed_label(TENANT_NAME, HASH_A, "Acme Corp")
    assert row is not None and row.label == "Acme Corp"
    assert store.get(TENANT_NAME, HASH_A).label == "Acme Corp"


def test_record_observed_label_is_fail_soft(store: FakeStore) -> None:
    """A bad label on a span is dropped, never raised: it must not fail an ingest.

    Telemetry is the product. A customer's display name is not worth dropping a span over.
    """
    assert store.record_observed_label(TENANT_NAME, HASH_A, None) is None
    assert store.record_observed_label(TENANT_NAME, HASH_A, "") is None
    assert store.record_observed_label(TENANT_NAME, HASH_A, 42) is None
    assert store.record_observed_label(TENANT_NAME, HASH_A, "z" * 5000) is None
    assert store.get(TENANT_NAME, HASH_A) is None


def test_absent_attribute_does_not_clear_an_existing_label(store: FakeStore) -> None:
    """A span without the attribute says nothing about whether the account should keep its name."""
    store.upsert(TENANT_NAME, HASH_A, label="Acme Corp")
    store.record_observed_label(TENANT_NAME, HASH_A, None)
    assert store.get(TENANT_NAME, HASH_A).label == "Acme Corp"
