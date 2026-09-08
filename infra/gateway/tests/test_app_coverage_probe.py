# SPDX-License-Identifier: Apache-2.0
"""Per-layer instrumentation coverage (CTO-261, onboarding-agent §7).

The tests that matter are the honesty ones: a layer with no span is never ``covered``, and an
unreachable ClickHouse reads as ``unknown`` rather than as a report that the layer is dark.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.coverage_probe import (
    AccountSignal,
    LAYERS,
    build_coverage,
    normalize_wired,
    parse_wired_param,
)

T = "t-acme"


class FakeStore:
    """Stands in for ClickHouseStore. ``raise_*`` simulates an unreachable store."""

    def __init__(
        self,
        operation_counts: dict[str, int] | None = None,
        account_rows: tuple[int, int] = (0, 0),
        raise_spans: bool = False,
        raise_account: bool = False,
    ) -> None:
        self._operation_counts = operation_counts or {}
        self._account_rows = account_rows
        self._raise_spans = raise_spans
        self._raise_account = raise_account

    def coverage_operation_counts(self, tenant_id: str) -> dict[str, int]:
        if self._raise_spans:
            raise RuntimeError("clickhouse unreachable")
        return dict(self._operation_counts)

    def coverage_account_rows(self, tenant_id: str) -> tuple[int, int]:
        if self._raise_account:
            raise RuntimeError("clickhouse unreachable")
        return self._account_rows

    def close(self) -> None:
        """The app's shutdown hook closes the store; the fake has nothing to close."""


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _states(body: dict) -> dict[str, str]:
    return {row["layer"]: row["state"] for row in body["layers"]}


def _reasons(body: dict) -> dict[str, str]:
    return {row["layer"]: row["reason"] for row in body["layers"]}


# --------------------------------------------------------------------------------------------
# The pure mapper.
# --------------------------------------------------------------------------------------------


def test_every_layer_is_reported() -> None:
    rows = build_coverage({}, AccountSignal(0, 0))
    assert [r.layer for r in rows] == list(LAYERS)


def test_a_layer_with_a_span_is_covered_with_the_proving_count() -> None:
    rows = {r.layer: r for r in build_coverage({"tool": 3}, AccountSignal(0, 0))}
    assert rows["tools"].state == "covered"
    assert rows["tools"].proving_spans == 3
    assert "GenAiOperation = 'tool'" in rows["tools"].reason


def test_a_layer_with_no_span_is_never_covered() -> None:
    """The central rule of §7: no proving span, no coverage. Not even when the caller claims it."""
    rows = {
        r.layer: r
        for r in build_coverage({"chat": 12}, AccountSignal(0, 0), normalize_wired(LAYERS))
    }
    for layer in ("tools", "vector", "embeddings", "account"):
        assert rows[layer].state != "covered", layer
    for layer in ("tools", "vector", "embeddings"):
        assert rows[layer].proving_spans == 0, layer


def test_wired_but_unexercised_is_distinct_from_not_wired() -> None:
    rows = {
        r.layer: r
        for r in build_coverage({"chat": 1}, AccountSignal(2, 0), normalize_wired(["vector"]))
    }
    assert rows["vector"].state == "awaiting_first_event"
    assert "awaiting first event" in rows["vector"].reason
    assert rows["tools"].state == "not_wired"


def test_an_unreadable_span_probe_is_unknown_not_not_covered() -> None:
    rows = {r.layer: r for r in build_coverage(None, AccountSignal(5, 5))}
    for layer in ("llm", "tools", "vector", "embeddings"):
        assert rows[layer].state == "unknown", layer
        # An unknown count is null, never 0 (CLAUDE.md).
        assert rows[layer].proving_spans is None
        assert "cannot tell" in rows[layer].reason
    # The rollup answered, so attribution is still reported rather than blanked with it.
    assert rows["account"].state == "covered"


def test_an_unreadable_rollup_leaves_the_span_layers_intact() -> None:
    rows = {r.layer: r for r in build_coverage({"chat": 4}, None)}
    assert rows["llm"].state == "covered"
    assert rows["account"].state == "unknown"
    assert rows["account"].proving_spans is None


def test_attributed_rollup_rows_prove_the_account_layer() -> None:
    rows = {r.layer: r for r in build_coverage({"chat": 4}, AccountSignal(9, 2))}
    assert rows["account"].state == "covered"
    assert rows["account"].proving_spans == 2


def test_an_empty_rollup_under_a_tenant_with_spans_is_unknown() -> None:
    """A never-backfilled materialized view is our gap, not the developer's (CTO-261)."""
    rows = {r.layer: r for r in build_coverage({"chat": 40}, AccountSignal(0, 0))}
    assert rows["account"].state == "unknown"
    assert "rollup is behind" in rows["account"].reason


def test_an_empty_rollup_under_a_tenant_with_no_spans_is_a_real_gap() -> None:
    rows = {r.layer: r for r in build_coverage({}, AccountSignal(0, 0))}
    assert rows["account"].state == "not_wired"


def test_wired_claim_cannot_manufacture_coverage_from_garbage() -> None:
    assert parse_wired_param("tools, vector,nonsense,") == frozenset({"tools", "vector"})
    assert parse_wired_param(None) == frozenset()
    assert normalize_wired([1, None, "llm"]) == frozenset({"llm"})


# --------------------------------------------------------------------------------------------
# The endpoint.
# --------------------------------------------------------------------------------------------


def test_endpoint_reports_covered_and_dark_layers(client: TestClient) -> None:
    app.state.store = FakeStore(operation_counts={"chat": 7, "vector": 1}, account_rows=(3, 1))
    r = client.get(
        "/v1/tenant/onboarding/coverage?wired=tools", headers={"X-Tenant-Id": T}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == T
    assert _states(body) == {
        "llm": "covered",
        "tools": "awaiting_first_event",
        "vector": "covered",
        "embeddings": "not_wired",
        "account": "covered",
    }
    # Every dark layer is named with a reason, per §7.
    assert all(reason for reason in _reasons(body).values())


def test_endpoint_maps_an_unreachable_clickhouse_to_unknown(client: TestClient) -> None:
    """Never a 5xx, and never a row of zeroes passed off as "not covered"."""
    app.state.store = FakeStore(raise_spans=True, raise_account=True)
    r = client.get("/v1/tenant/onboarding/coverage", headers={"X-Tenant-Id": T})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(_states(body).values()) == {"unknown"}
    assert all(row["proving_spans"] is None for row in body["layers"])


def test_endpoint_requires_a_tenant_header(client: TestClient) -> None:
    app.state.store = FakeStore()
    assert client.get("/v1/tenant/onboarding/coverage").status_code == 422


def test_endpoint_is_gated_on_the_service_token(client: TestClient) -> None:
    """Initiative 1 §6: the control plane answers the web server, not the internet."""
    app.state.store = FakeStore()
    settings = app.state.settings
    settings.require_api_key = True
    settings.gateway_service_token = "svc-secret"
    try:
        assert client.get(
            "/v1/tenant/onboarding/coverage", headers={"X-Tenant-Id": T}
        ).status_code == 401
        assert client.get(
            "/v1/tenant/onboarding/coverage",
            headers={"X-Tenant-Id": T, "Authorization": "Bearer wrong"},
        ).status_code == 401
        assert client.get(
            "/v1/tenant/onboarding/coverage",
            headers={"X-Tenant-Id": T, "Authorization": "Bearer svc-secret"},
        ).status_code == 200
    finally:
        settings.require_api_key = False
        settings.gateway_service_token = ""
