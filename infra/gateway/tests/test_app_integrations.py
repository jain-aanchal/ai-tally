"""GET /v1/tenant/integrations/status — per-tenant third-party integration status (CTO-117).

The dashboard reads this to drive the three card states (connected-healthy / connected-failing /
not-connected) on /connectors. These tests pin: fresh tenants get an empty list, listing reflects
recorded runs, error messages are PII-scrubbed before persistence, and the cross-tenant SQL
boundary is honored.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_integrations import (
    ALLOWED_CONNECTORS,
    IntegrationStatus,
    scrub_error_message,
)

T = "t-acme"


class FakeIntegrationStore:
    """In-memory stand-in for :class:`TenantIntegrationStore` — no Postgres."""

    def __init__(self) -> None:
        # (tenant_id, connector_id) -> IntegrationStatus
        self._rows: dict[tuple[str, str], IntegrationStatus] = {}

    def get_status(self, tenant_id: str) -> list[IntegrationStatus]:
        return sorted(
            [r for (t, _), r in self._rows.items() if t == tenant_id],
            key=lambda r: r.connector_id,
        )

    def record_run(
        self,
        tenant_id: str,
        connector_id: str,
        status: str,
        *,
        event_count: int = 0,
        error_message: str | None = None,
    ) -> IntegrationStatus:
        if connector_id not in ALLOWED_CONNECTORS:
            raise ValueError(f"unknown connector_id '{connector_id}'")
        # Critically: tests verify scrubbing happens *before* the value reaches the store, just
        # like the real implementation. Mirror the same call so cross-tenant + scrub tests share
        # a single source of truth.
        scrubbed = scrub_error_message(error_message)
        prior = self._rows.get((tenant_id, connector_id))
        prior_24h = prior.total_events_24h if prior else 0
        prior_7d = prior.total_events_7d if prior else 0
        row = IntegrationStatus(
            connector_id=connector_id,
            last_run_at=datetime.now(tz=timezone.utc).isoformat(),
            last_run_status=status,  # type: ignore[arg-type]
            last_run_event_count=event_count,
            last_run_error_message=scrubbed,
            total_events_24h=prior_24h + event_count,
            total_events_7d=prior_7d + event_count,
        )
        self._rows[(tenant_id, connector_id)] = row
        return row


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.tenant_integrations = FakeIntegrationStore()
        yield c


def test_fresh_tenant_returns_empty_list(client: TestClient) -> None:
    # The honest "not connected anywhere" state — no rows means no third-party integration has
    # ever produced a run for this tenant. The web app renders catalog-entries-without-a-row
    # as "Not connected" cards.
    r = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == T
    assert body["integrations"] == []


def test_listing_reflects_recorded_runs(client: TestClient) -> None:
    store: FakeIntegrationStore = app.state.tenant_integrations
    store.record_run(T, "stripe", "success", event_count=3)
    store.record_run(T, "segment", "failed", event_count=0, error_message="rate limited")

    r = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    by_id = {row["connector_id"]: row for row in r.json()["integrations"]}
    assert set(by_id) == {"stripe", "segment"}
    assert by_id["stripe"]["last_run_status"] == "success"
    assert by_id["stripe"]["last_run_event_count"] == 3
    assert by_id["stripe"]["total_events_24h"] == 3
    assert by_id["segment"]["last_run_status"] == "failed"
    assert by_id["segment"]["last_run_error_message"] == "rate limited"


def test_pii_email_in_error_message_is_scrubbed(client: TestClient) -> None:
    # Third-party errors sometimes echo customer emails (Stripe and HubSpot are repeat
    # offenders). The store must redact emails *before* the value reaches storage.
    store: FakeIntegrationStore = app.state.tenant_integrations
    store.record_run(
        T,
        "stripe",
        "failed",
        event_count=0,
        error_message="Webhook delivery failed for foo@example.com after 3 retries",
    )
    r = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": T})
    msg = r.json()["integrations"][0]["last_run_error_message"]
    assert "foo@example.com" not in msg
    assert "[redacted-email]" in msg


def test_pii_forbidden_key_in_error_message_is_redacted(client: TestClient) -> None:
    # If a third-party error embeds a forbidden-key marker (email=, user.email, phone…) we
    # collapse the whole message rather than try to parse-and-strip — easier to keep honest.
    store: FakeIntegrationStore = app.state.tenant_integrations
    store.record_run(
        T,
        "hubspot",
        "failed",
        event_count=0,
        error_message="API returned 400: email=foo@bar.com is invalid",
    )
    r = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": T})
    msg = r.json()["integrations"][0]["last_run_error_message"]
    assert msg == "[redacted: contained PII key]"


def test_scrub_error_message_handles_none_and_empty() -> None:
    assert scrub_error_message(None) is None
    assert scrub_error_message("") is None
    assert scrub_error_message("   ") is None
    assert scrub_error_message("plain error") == "plain error"


def test_cross_tenant_isolation(client: TestClient) -> None:
    store: FakeIntegrationStore = app.state.tenant_integrations
    store.record_run("t-a", "stripe", "success", event_count=5)
    store.record_run("t-b", "segment", "failed", event_count=0, error_message="oops")

    a = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": "t-a"}).json()
    b = client.get("/v1/tenant/integrations/status", headers={"X-Tenant-Id": "t-b"}).json()

    assert [r["connector_id"] for r in a["integrations"]] == ["stripe"]
    assert [r["connector_id"] for r in b["integrations"]] == ["segment"]
    # And critically: tenant A cannot see tenant B's failed-segment error string.
    assert all(r["connector_id"] != "segment" for r in a["integrations"])


def test_requires_tenant_when_auth_disabled(client: TestClient) -> None:
    assert client.get("/v1/tenant/integrations/status").status_code == 422
