"""GET/POST /v1/tenant/allocation-config: the shared-cost allocation rule (CTO-193, plan C2).

Three tests carry the acceptance criteria:

* :func:`test_tenant_with_no_row_gets_the_default_and_says_so`: the state every tenant on the
  system is in today. It must return a usable rule AND report that nobody chose it.
* :func:`test_unknown_rule_is_rejected_not_defaulted`: storing one rule and applying another
  would make the rule named on the page a lie about the numbers beside it.
* :func:`test_name_based_caller_does_not_500`: the tenant name-vs-UUID trap. The table keys on
  ``tenants.id`` but the dashboard sends ``local-dev``.

The rest guard idempotent replay and the audit trail around them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_allocation import (
    ALLOCATION_RULES,
    DEFAULT_ALLOCATION_RULE,
    AllocationConfig,
    AllocationConfigError,
    TenantNotFound,
    normalize_rule,
    normalize_updated_by,
)

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
UNKNOWN_TENANT = "no-such-tenant"


class FakeStore:
    """In-memory stand-in for :class:`TenantAllocationStore`, so no Postgres is needed.

    Mirrors the contract the endpoints depend on: absence means the default, a name and a UUID fold
    onto ONE row the way the foreign key forces, an unknown rule raises, and a replayed change_id
    is a no-op that leaves ``updated_at`` and the audit log alone.
    """

    def __init__(self, known_tenants: set[str] | None = None) -> None:
        self._rows: dict[str, AllocationConfig] = {}
        self._changes: list[tuple[str, str]] = []  # (tenant, change_id)
        self._known = (
            known_tenants if known_tenants is not None else {TENANT_NAME, TENANT_UUID}
        )

    def _resolve(self, tenant_id: str) -> str:
        if tenant_id not in self._known:
            raise TenantNotFound("no tenant matches the supplied identifier")
        return TENANT_UUID if tenant_id in {TENANT_NAME, TENANT_UUID} else tenant_id

    def get(self, tenant_id: str) -> AllocationConfig | None:
        return self._rows.get(self._resolve(tenant_id))

    def upsert(
        self,
        tenant_id: str,
        rule: str,
        *,
        change_id: str,
        updated_by: str | None = None,
    ) -> AllocationConfig:
        rule = normalize_rule(rule)
        updated_by = normalize_updated_by(updated_by)
        resolved = self._resolve(tenant_id)
        existing = self._rows.get(resolved)
        if (resolved, change_id) in self._changes:
            assert existing is not None
            return existing
        self._changes.append((resolved, change_id))
        now = datetime.now(tz=timezone.utc)
        row = AllocationConfig(
            allocation_rule=rule,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            updated_by=updated_by,
        )
        self._rows[resolved] = row
        return row

    def change_count(self, tenant_id: str) -> int:
        resolved = self._resolve(tenant_id)
        return sum(1 for t, _ in self._changes if t == resolved)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_allocation = store
        yield c


def _get(client: TestClient, tenant: str = TENANT_NAME):
    return client.get("/v1/tenant/allocation-config", headers={"X-Tenant-Id": tenant})


def _post(client: TestClient, body: dict, tenant: str = TENANT_NAME):
    return client.post(
        "/v1/tenant/allocation-config", headers={"X-Tenant-Id": tenant}, json=body
    )


# --- the acceptance paths ------------------------------------------------------------------------


def test_tenant_with_no_row_gets_the_default_and_says_so(client: TestClient) -> None:
    """No row is a supported state, not missing config.

    Every tenant on the system is in this state today, so the endpoint has to answer usefully:
    name a rule the page can actually apply, and separately admit that nobody chose it. Collapsing
    the two would present the default as a decision somebody made.
    """
    res = _get(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allocation_rule"] == DEFAULT_ALLOCATION_RULE == "pro_rata_direct"
    assert body["configured"] is False
    assert body["config"] is None
    assert body["available_rules"] == list(ALLOCATION_RULES)


def test_unknown_rule_is_rejected_not_defaulted(client: TestClient) -> None:
    """An unknown rule is a 422 and writes nothing.

    Falling back to the default here would store one rule and apply another, leaving the page
    naming a rule that did not produce the numbers beside it. That is the exact invisible
    assumption this config exists to remove, so a rejected write is the better failure.
    """
    res = _post(
        client,
        {"allocation_rule": "pro_rata_tokens", "change_id": str(uuid.uuid4())},
    )
    assert res.status_code == 422, res.text
    assert "pro_rata_direct" in res.json()["detail"]
    assert _get(client).json()["configured"] is False


def test_name_based_caller_does_not_500(client: TestClient) -> None:
    """A tenant NAME and its UUID address the same row.

    The table keys on ``tenants.id`` but the dashboard identifies a tenant as ``local-dev``.
    Without the resolver a name-based caller trips InvalidTextRepresentation deep in the driver and
    surfaces as an opaque 503, which is the trap that has caught three features in this stack.
    """
    written = _post(
        client, {"allocation_rule": "even_split", "change_id": str(uuid.uuid4())}
    )
    assert written.status_code == 200, written.text

    by_name = _get(client, TENANT_NAME)
    by_uuid = _get(client, TENANT_UUID)
    assert by_name.status_code == 200 and by_uuid.status_code == 200
    assert by_name.json()["allocation_rule"] == "even_split"
    assert by_uuid.json()["allocation_rule"] == "even_split", (
        "both spellings must address one row, or a tenant configures the rule through one door "
        "and reads the default through the other"
    )


# --- invariants around them ----------------------------------------------------------------------


def test_upsert_round_trips_and_reports_configured(client: TestClient) -> None:
    res = _post(
        client,
        {
            "allocation_rule": "even_split",
            "change_id": str(uuid.uuid4()),
            "updated_by": "finance@acme.test",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["allocation_rule"] == "even_split"
    assert res.json()["configured"] is True

    read = _get(client).json()
    assert read["allocation_rule"] == "even_split"
    assert read["configured"] is True
    assert read["config"]["updated_by"] == "finance@acme.test"


def test_replayed_change_id_writes_once(client: TestClient, store: FakeStore) -> None:
    """A retried POST must not append a second audit row claiming a change nobody made."""
    change_id = str(uuid.uuid4())
    body = {"allocation_rule": "even_split", "change_id": change_id}
    first = _post(client, body)
    second = _post(client, body)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["allocation_rule"] == "even_split"
    assert store.change_count(TENANT_NAME) == 1


def test_change_id_is_required(client: TestClient) -> None:
    res = _post(client, {"allocation_rule": "even_split"})
    assert res.status_code == 422
    assert "change_id" in res.json()["detail"]


def test_unknown_tenant_is_404_not_a_default(client: TestClient) -> None:
    """An unknown tenant is a misrouted request, not a tenant that wants the default."""
    res = _get(client, UNKNOWN_TENANT)
    assert res.status_code == 404, res.text


def test_every_available_rule_is_actually_storable(client: TestClient) -> None:
    """The advertised list and the accepted list are the same list.

    A config surface renders ``available_rules``, so a rule advertised there but rejected by the
    write would be a dead option in a dropdown.
    """
    for rule in ALLOCATION_RULES:
        res = _post(client, {"allocation_rule": rule, "change_id": str(uuid.uuid4())})
        assert res.status_code == 200, f"{rule}: {res.text}"
        assert res.json()["allocation_rule"] == rule


# --- normalizers ---------------------------------------------------------------------------------


def test_normalize_rule_trims_and_lowercases() -> None:
    assert normalize_rule("  Even_Split ") == "even_split"


@pytest.mark.parametrize("bad", [None, 42, "", "   ", "pro rata", "ignore_shared"])
def test_normalize_rule_rejects(bad: object) -> None:
    with pytest.raises(AllocationConfigError):
        normalize_rule(bad)


def test_normalize_updated_by_blank_is_none() -> None:
    assert normalize_updated_by("   ") is None
    assert normalize_updated_by(None) is None


def test_normalize_updated_by_rejects_oversize() -> None:
    with pytest.raises(AllocationConfigError):
        normalize_updated_by("x" * 5000)
