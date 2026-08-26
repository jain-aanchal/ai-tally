# SPDX-License-Identifier: Apache-2.0
"""GET/POST/DELETE /v1/tenant/budgets: what a tenant intends to spend (CTO-205, F1).

Four tests carry the acceptance criteria:

* :func:`test_tenant_with_no_budget_is_a_normal_state`: the state every tenant is in today. It must
  answer 200 with an empty list and ``configured: false``, never a 404 and never an implicit zero.
* :func:`test_scoped_budgets_round_trip`: feature, model and layer budgets, which are how teams
  actually govern spend and what makes the later burn-down useful to a feature owner.
* :func:`test_overlapping_budget_is_rejected_at_write_time`: the decision of this ticket. Two
  budgets covering one scope on one day would leave the burn-down with no principled answer.
* :func:`test_name_based_caller_does_not_500`: the tenant name-vs-UUID trap (CTO-201). The table
  keys on ``tenants.id`` but the dashboard sends ``local-dev``.

The rest guard the money contract (integer micro-USD, never float dollars) and the normalizers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_budgets import (
    BUDGET_PERIODS,
    BUDGET_SCOPE_KINDS,
    Budget,
    BudgetError,
    BudgetOverlapError,
    TenantNotFound,
    normalize_amount_micro,
    normalize_budget_id,
    normalize_date,
    normalize_optional_date,
    normalize_period,
    normalize_scope,
)

TENANT_NAME = "local-dev"
TENANT_UUID = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
UNKNOWN_TENANT = "no-such-tenant"

# $30,000 in micro-USD. Spelled out once here so the tests read as money rather than as a
# suspiciously long integer.
THIRTY_K_MICRO = 30_000_000_000


def _overlaps(
    a_start: date, a_end: date | None, b_start: date, b_end: date | None
) -> bool:
    """Inclusive date-range overlap, mirroring the ``daterange(..., '[]')`` in migration 0026.

    ``None`` is 'infinity', i.e. open-ended, so two open-ended budgets always overlap.
    """
    if a_end is not None and a_end < b_start:
        return False
    if b_end is not None and b_end < a_start:
        return False
    return True


class FakeStore:
    """In-memory stand-in for :class:`TenantBudgetStore`, so no Postgres is needed.

    Mirrors the contract the endpoints depend on: a name and a UUID fold onto ONE tenant the way
    the foreign key forces, absence of a row is a normal empty answer, and an overlapping write is
    refused with the colliding budget_id the way the EXCLUDE constraint plus pre-check do.
    """

    def __init__(self, known_tenants: set[str] | None = None) -> None:
        self._rows: dict[tuple[str, str], Budget] = {}
        self._known = (
            known_tenants if known_tenants is not None else {TENANT_NAME, TENANT_UUID}
        )

    def _resolve(self, tenant_id: str) -> str:
        if tenant_id not in self._known:
            raise TenantNotFound("no tenant matches the supplied identifier")
        return TENANT_UUID

    def list(self, tenant_id: str) -> list[Budget]:
        resolved = self._resolve(tenant_id)
        rows = [b for (t, _), b in self._rows.items() if t == resolved]
        return sorted(
            rows, key=lambda b: (b.scope_kind, b.scope_value, b.starts_on, b.budget_id)
        )

    def get(self, tenant_id: str, budget_id: str) -> Budget | None:
        return self._rows.get((self._resolve(tenant_id), normalize_budget_id(budget_id)))

    def upsert(
        self,
        tenant_id: str,
        *,
        budget_id: str,
        period: str,
        amount_micro: int,
        scope_kind: str,
        starts_on: object,
        scope_value: object = "",
        ends_on: object = None,
    ) -> Budget:
        budget_id = normalize_budget_id(budget_id)
        period = normalize_period(period)
        amount_micro = normalize_amount_micro(amount_micro)
        scope_kind, scope_value = normalize_scope(scope_kind, scope_value)
        start = normalize_date(starts_on, field="starts_on")
        end = normalize_optional_date(ends_on, field="ends_on")
        if end is not None and end < start:
            raise BudgetError("ends_on must be on or after starts_on")
        resolved = self._resolve(tenant_id)

        for (t, bid), other in self._rows.items():
            if t != resolved or bid == budget_id:
                continue
            same_scope = (
                other.period == period
                and other.scope_kind == scope_kind
                and other.scope_value == scope_value
            )
            if same_scope and _overlaps(other.starts_on, other.ends_on, start, end):
                raise BudgetOverlapError(
                    "a budget already covers this scope and period over an overlapping date "
                    f"range: {bid}",
                    conflicting_budget_id=bid,
                )

        now = datetime.now(tz=timezone.utc)
        existing = self._rows.get((resolved, budget_id))
        row = Budget(
            budget_id=budget_id,
            period=period,
            amount_micro=amount_micro,
            scope_kind=scope_kind,
            scope_value=scope_value,
            starts_on=start,
            ends_on=end,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._rows[(resolved, budget_id)] = row
        return row

    def delete(self, tenant_id: str, budget_id: str) -> bool:
        resolved = self._resolve(tenant_id)
        return self._rows.pop((resolved, normalize_budget_id(budget_id)), None) is not None


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_budgets = store
        yield c


def _get(client: TestClient, tenant: str = TENANT_NAME):
    return client.get("/v1/tenant/budgets", headers={"X-Tenant-Id": tenant})


def _post(client: TestClient, body: dict, tenant: str = TENANT_NAME):
    return client.post(
        "/v1/tenant/budgets", headers={"X-Tenant-Id": tenant}, json=body
    )


def _delete(client: TestClient, budget_id: str, tenant: str = TENANT_NAME):
    return client.request(
        "DELETE",
        "/v1/tenant/budgets",
        headers={"X-Tenant-Id": tenant},
        json={"budget_id": budget_id},
    )


def _tenant_wide(**over: object) -> dict:
    body: dict = {
        "budget_id": "company-monthly",
        "period": "month",
        "amount_micro": THIRTY_K_MICRO,
        "scope_kind": "tenant",
        "starts_on": "2026-01-01",
    }
    body.update(over)
    return body


# --- the acceptance paths -------------------------------------------------------------------


def test_tenant_with_no_budget_is_a_normal_state(client: TestClient) -> None:
    """No budget row is a supported state, not an error and not a budget of zero.

    Every tenant on this system is in this state right now. A 404 here, or an ``amount_micro`` of
    0 invented to fill the gap, would make the whole forecasting epic report every tenant as
    infinitely over budget the moment they spend a cent.
    """
    res = _get(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["budgets"] == []
    assert body["configured"] is False
    assert body["available_periods"] == list(BUDGET_PERIODS)
    assert body["available_scope_kinds"] == list(BUDGET_SCOPE_KINDS)


def test_a_stored_zero_is_not_the_same_as_no_budget(client: TestClient) -> None:
    """Zero is a real budget ("this scope may spend nothing"), absence is not.

    The two have to stay distinguishable or the honest-under-uncertainty rule collapses: unknown
    renders null, never 0.
    """
    res = _post(client, _tenant_wide(amount_micro=0))
    assert res.status_code == 200, res.text
    assert res.json()["budget"]["amount_micro"] == 0
    read = _get(client).json()
    assert read["configured"] is True
    assert read["budgets"][0]["amount_micro"] == 0


@pytest.mark.parametrize(
    ("scope_kind", "scope_value"),
    [("feature", "research-agent"), ("model", "gpt-4o"), ("layer", "compute")],
)
def test_scoped_budgets_round_trip(
    client: TestClient, scope_kind: str, scope_value: str
) -> None:
    """A budget can name a feature, a model or a layer, not just the whole bill.

    "The research agent gets $30k a month" is how teams actually govern AI spend, and it is what
    makes the later burn-down useful to a feature owner rather than only to finance.
    """
    res = _post(
        client,
        _tenant_wide(
            budget_id=f"{scope_kind}-budget",
            scope_kind=scope_kind,
            scope_value=scope_value,
        ),
    )
    assert res.status_code == 200, res.text
    budget = res.json()["budget"]
    assert budget["scope_kind"] == scope_kind
    assert budget["scope_value"] == scope_value
    assert budget["amount_micro"] == THIRTY_K_MICRO
    assert budget["ends_on"] is None, "an absent end date means open-ended, not unknown"


def test_scoped_budgets_are_independent_of_the_tenant_wide_one(
    client: TestClient,
) -> None:
    """A feature budget and a tenant-wide budget for the same month do not collide.

    They are different scopes, so both apply and neither is ambiguous. If overlap rejection were
    keyed on the period alone, a tenant could never budget a feature and the company at once, which
    is the single most common thing they will want to do.
    """
    assert _post(client, _tenant_wide()).status_code == 200
    scoped = _post(
        client,
        _tenant_wide(
            budget_id="research-agent",
            scope_kind="feature",
            scope_value="research-agent",
        ),
    )
    assert scoped.status_code == 200, scoped.text
    assert len(_get(client).json()["budgets"]) == 2


def test_overlapping_budget_is_rejected_at_write_time(client: TestClient) -> None:
    """Two budgets for one scope, period and day are refused, and the response names the clash.

    Resolving the ambiguity at read time has no principled tie-break (newest-wins silently
    disables a budget somebody set, smallest-wins turns a duplicate into a false breach, summing
    invents a budget nobody approved) and would have to be reimplemented identically in the
    projection, the chart and the future alerts.
    """
    assert _post(client, _tenant_wide(budget_id="q1", ends_on="2026-03-31")).status_code == 200

    clash = _post(
        client, _tenant_wide(budget_id="also-q1", starts_on="2026-02-01", ends_on="2026-04-30")
    )
    assert clash.status_code == 409, clash.text
    assert clash.json()["detail"]["conflicting_budget_id"] == "q1"
    assert len(_get(client).json()["budgets"]) == 1, "the refused write must store nothing"


def test_adjacent_budgets_are_allowed(client: TestClient) -> None:
    """A successor starting the day after the incumbent ends is not an overlap.

    Ranges are inclusive at both ends, so a budget ending on the 31st covers the 31st and its
    successor starts on the 1st. This is the ordinary "we raised the budget for Q2" path and it
    must not need a delete first.
    """
    assert _post(client, _tenant_wide(budget_id="q1", ends_on="2026-03-31")).status_code == 200
    q2 = _post(
        client, _tenant_wide(budget_id="q2", starts_on="2026-04-01", ends_on="2026-06-30")
    )
    assert q2.status_code == 200, q2.text
    assert len(_get(client).json()["budgets"]) == 2


def test_editing_a_budget_in_place_does_not_collide_with_itself(
    client: TestClient,
) -> None:
    """A budget always overlaps itself, so an edit has to be exempt from its own overlap check.

    Without the self-exclusion no budget could ever be corrected, which would make a typo in an
    amount permanent short of a delete.
    """
    assert _post(client, _tenant_wide()).status_code == 200
    edited = _post(client, _tenant_wide(amount_micro=45_000_000_000))
    assert edited.status_code == 200, edited.text
    assert edited.json()["budget"]["amount_micro"] == 45_000_000_000
    assert len(_get(client).json()["budgets"]) == 1


def test_two_open_ended_budgets_for_one_scope_collide(client: TestClient) -> None:
    """Open-ended means 'until further notice', so a second one necessarily overlaps the first."""
    assert _post(client, _tenant_wide(budget_id="standing")).status_code == 200
    second = _post(client, _tenant_wide(budget_id="standing-2", starts_on="2027-01-01"))
    assert second.status_code == 409, second.text


def test_name_based_caller_does_not_500(client: TestClient) -> None:
    """A tenant NAME and its UUID address the same rows.

    ``tenant_budgets`` keys on ``tenants.id`` but the dashboard identifies a tenant as
    ``local-dev``. Without the resolver a name-based caller trips InvalidTextRepresentation deep in
    the driver and surfaces as an opaque 503, which is the trap CTO-201 was filed over.
    """
    written = _post(client, _tenant_wide(), tenant=TENANT_NAME)
    assert written.status_code == 200, written.text

    by_name = _get(client, TENANT_NAME)
    by_uuid = _get(client, TENANT_UUID)
    assert by_name.status_code == 200 and by_uuid.status_code == 200, by_uuid.text
    assert by_name.json()["budgets"] == by_uuid.json()["budgets"], (
        "both spellings must address one tenant, or a budget set through one door is invisible "
        "through the other"
    )


def test_overlap_is_detected_across_tenant_spellings(client: TestClient) -> None:
    """A budget written by name must collide with an overlapping one written by UUID.

    If the resolver only worked on the read path, the two spellings would be two tenants for the
    purposes of the overlap check and the invariant would quietly not hold.
    """
    assert _post(client, _tenant_wide(budget_id="by-name"), tenant=TENANT_NAME).status_code == 200
    clash = _post(client, _tenant_wide(budget_id="by-uuid"), tenant=TENANT_UUID)
    assert clash.status_code == 409, clash.text


# --- the money contract ---------------------------------------------------------------------


def test_float_dollars_are_rejected(client: TestClient) -> None:
    """Money is an integer of micro-USD at the boundary, and a float is a 422 rather than a round.

    Accepting ``30000.0`` would advertise dollars-as-float as a supported shape, and the next
    caller passes ``30000.5`` and silently loses half a cent per budget against cost figures that
    are already integers.
    """
    res = _post(client, _tenant_wide(amount_micro=30000.5))
    assert res.status_code == 422, res.text
    assert "micro-USD" in res.json()["detail"]


def test_negative_amount_is_rejected(client: TestClient) -> None:
    assert _post(client, _tenant_wide(amount_micro=-1)).status_code == 422


def test_implausible_amount_is_rejected(client: TestClient) -> None:
    """A misplaced decimal produces a number that looks real and makes every variance nonsense."""
    res = _post(client, _tenant_wide(amount_micro=10**18))
    assert res.status_code == 422
    assert "decimal" in res.json()["detail"]


# --- validation and lifecycle ----------------------------------------------------------------


def test_unknown_period_is_rejected(client: TestClient) -> None:
    res = _post(client, _tenant_wide(period="fortnight"))
    assert res.status_code == 422
    assert "month" in res.json()["detail"]


def test_tenant_scope_must_not_name_a_value(client: TestClient) -> None:
    """'tenant'/'checkout' is ambiguous about what it actually covers."""
    res = _post(client, _tenant_wide(scope_kind="tenant", scope_value="checkout"))
    assert res.status_code == 422


def test_scoped_budget_requires_a_value(client: TestClient) -> None:
    """'feature'/'' could never be matched against a cost series, so it is not storable."""
    res = _post(client, _tenant_wide(scope_kind="feature", scope_value=""))
    assert res.status_code == 422


def test_end_before_start_is_rejected(client: TestClient) -> None:
    res = _post(client, _tenant_wide(starts_on="2026-03-01", ends_on="2026-02-01"))
    assert res.status_code == 422


def test_bad_date_is_rejected(client: TestClient) -> None:
    res = _post(client, _tenant_wide(starts_on="March 2026"))
    assert res.status_code == 422


def test_delete_removes_the_budget(client: TestClient) -> None:
    assert _post(client, _tenant_wide()).status_code == 200
    removed = _delete(client, "company-monthly")
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True
    assert _get(client).json()["configured"] is False


def test_delete_of_an_absent_budget_is_not_an_error(client: TestClient) -> None:
    """The end state the caller asked for is the end state they get. A double-click is not a 404."""
    res = _delete(client, "never-existed")
    assert res.status_code == 200, res.text
    assert res.json()["removed"] is False


def test_delete_accepts_a_query_parameter(client: TestClient) -> None:
    """DELETE-with-a-body is awkward from several HTTP clients, so the query param works too."""
    assert _post(client, _tenant_wide()).status_code == 200
    res = client.request(
        "DELETE",
        "/v1/tenant/budgets?budget_id=company-monthly",
        headers={"X-Tenant-Id": TENANT_NAME},
    )
    assert res.status_code == 200, res.text
    assert res.json()["removed"] is True


def test_delete_without_a_budget_id_is_422(client: TestClient) -> None:
    res = client.request(
        "DELETE", "/v1/tenant/budgets", headers={"X-Tenant-Id": TENANT_NAME}
    )
    assert res.status_code == 422


def test_unknown_tenant_is_404_not_an_empty_list(client: TestClient) -> None:
    """A misrouted request is not a tenant who has set no budgets."""
    assert _get(client, UNKNOWN_TENANT).status_code == 404


def test_list_is_ordered_by_scope_then_start(client: TestClient) -> None:
    """A settings UI renders this list directly, so the order has to be stable across reads."""
    assert _post(client, _tenant_wide(budget_id="b")).status_code == 200
    assert _post(
        client,
        _tenant_wide(budget_id="a", scope_kind="feature", scope_value="research-agent"),
    ).status_code == 200
    kinds = [b["scope_kind"] for b in _get(client).json()["budgets"]]
    assert kinds == ["feature", "tenant"]


# --- normalizers ------------------------------------------------------------------------------


def test_normalize_budget_id_trims_but_does_not_case_fold() -> None:
    """It is the primary key, so rewriting it would leave a caller unable to address its own row."""
    assert normalize_budget_id("  Research-Agent  ") == "Research-Agent"


@pytest.mark.parametrize("bad", [None, 42, "", "   ", "x" * 500])
def test_normalize_budget_id_rejects(bad: object) -> None:
    with pytest.raises(BudgetError):
        normalize_budget_id(bad)


def test_normalize_period_trims_and_lowercases() -> None:
    assert normalize_period("  Month ") == "month"


@pytest.mark.parametrize("bad", [None, 42, "", "year", "weekly"])
def test_normalize_period_rejects(bad: object) -> None:
    with pytest.raises(BudgetError):
        normalize_period(bad)


def test_normalize_amount_rejects_bool() -> None:
    """``True`` is an int in Python, and a budget of True is not a budget."""
    with pytest.raises(BudgetError):
        normalize_amount_micro(True)


def test_normalize_scope_preserves_case_of_the_value() -> None:
    """A model id or FeatureId is compared against telemetry that preserves case."""
    assert normalize_scope("Model", "GPT-4o") == ("model", "GPT-4o")


def test_normalize_scope_treats_none_as_empty() -> None:
    assert normalize_scope("tenant", None) == ("tenant", "")


@pytest.mark.parametrize("bad_kind", [None, 42, "", "account", "team"])
def test_normalize_scope_rejects_unknown_kind(bad_kind: object) -> None:
    with pytest.raises(BudgetError):
        normalize_scope(bad_kind, "x")


def test_normalize_optional_date_blank_is_open_ended() -> None:
    assert normalize_optional_date(None, field="ends_on") is None
    assert normalize_optional_date("   ", field="ends_on") is None


def test_normalize_date_accepts_a_date_object() -> None:
    assert normalize_date(date(2026, 1, 1), field="starts_on") == date(2026, 1, 1)


def test_normalize_date_rejects_a_timestamp() -> None:
    """A budget boundary is a calendar day. A timestamp would silently drop its time component."""
    with pytest.raises(BudgetError):
        normalize_date(datetime(2026, 1, 1, 12, 0), field="starts_on")
