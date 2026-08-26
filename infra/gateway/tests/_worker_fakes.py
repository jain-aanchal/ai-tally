"""Shared in-memory fakes for the CTO-127 ingest-worker tests.

No Postgres, no ClickHouse, no network — every dependency the workers take is stubbed here so the
worker logic (mapping, record_run bookkeeping, honest failure handling) is exercised in isolation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gateway.tenant_integration_secrets import IntegrationSecret
from gateway.tenant_integrations import IntegrationStatus, scrub_error_message
from tally.wire import BusinessEvent, IdentityLink


class FakeSecretStore:
    """Returns a single canned :class:`IntegrationSecret` (or ``None`` = not connected)."""

    def __init__(self, secret: IntegrationSecret | None) -> None:
        self._secret = secret

    def get(self, tenant_id: str, connector_id: str) -> IntegrationSecret | None:
        return self._secret


class FakeResolver:
    """Resolves ``secret_ref`` from an in-memory dict; raises like a real resolver on a miss."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}

    def resolve(self, secret_ref: str) -> str:
        if secret_ref not in self._mapping:
            raise KeyError(f"no credential resolvable for reference {secret_ref!r}")
        return self._mapping[secret_ref]


class FakeHttp:
    """Injectable HTTP client. Returns ``payload`` or raises ``error`` — never touches a socket."""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def get_json(self, url: str, *, headers: Any = None, params: Any = None) -> Any:
        self.calls.append({"url": url, "headers": dict(headers or {}), "params": params})
        if self._error is not None:
            raise self._error
        return self._payload


class FakeCHStore:
    """Captures business-event / identity-link inserts (the de-facto ClickHouse fake shape)."""

    def __init__(self) -> None:
        self.events: list[BusinessEvent] = []
        self.links: list[IdentityLink] = []

    def insert_business_events(self, tenant_id: str, events: list[BusinessEvent]) -> int:
        self.events.extend(events)
        return len(events)

    def insert_identity_links(self, tenant_id: str, links: list[IdentityLink]) -> int:
        self.links.extend(links)
        return len(links)


class FakeIntegrations:
    """Captures ``record_run`` calls; can be told to raise to test best-effort swallowing.

    Applies the real :func:`scrub_error_message` so the recorded error mirrors production — but the
    worker already scrubs before calling, so this is just a second (idempotent) pass.
    """

    def __init__(self, *, raise_on_record: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_on_record

    def record_run(
        self,
        tenant_id: str,
        connector_id: str,
        status: str,
        *,
        event_count: int = 0,
        error_message: str | None = None,
    ) -> IntegrationStatus:
        if self._raise:
            raise RuntimeError("postgres unavailable")
        scrubbed = scrub_error_message(error_message)
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "connector_id": connector_id,
                "status": status,
                "event_count": event_count,
                "error_message": scrubbed,
            }
        )
        return IntegrationStatus(
            connector_id=connector_id,
            last_run_at="2026-07-12T00:00:00+00:00",
            last_run_status=status,  # type: ignore[arg-type]
            last_run_event_count=event_count,
            last_run_error_message=scrubbed,
            total_events_24h=event_count,
            total_events_7d=event_count,
        )


class FakeCursorStore:
    """In-memory ``CursorStore`` (CTO-219). Same monotonic-advance contract as the real one."""

    def __init__(self, initial: dict[tuple[str, str], datetime] | None = None) -> None:
        self.cursors: dict[tuple[str, str], datetime] = dict(initial or {})
        self.advances: list[tuple[str, str, datetime]] = []
        self.raise_on_get = False
        self.raise_on_advance = False

    def get(self, tenant_id: str, connector_id: str) -> datetime | None:
        if self.raise_on_get:
            raise RuntimeError("postgres unavailable")
        return self.cursors.get((tenant_id, connector_id))

    def advance(self, tenant_id: str, connector_id: str, cursor_at: datetime) -> datetime:
        if self.raise_on_advance:
            raise RuntimeError("postgres unavailable")
        self.advances.append((tenant_id, connector_id, cursor_at))
        key = (tenant_id, connector_id)
        existing = self.cursors.get(key)
        self.cursors[key] = cursor_at if existing is None else max(existing, cursor_at)
        return self.cursors[key]


def make_secret(connector_id: str, *, ref: str = "ref-1", **config: Any) -> IntegrationSecret:
    return IntegrationSecret(
        tenant_id="t-acme",
        connector_id=connector_id,
        secret_ref=ref,
        config=dict(config),
        connected_at="2026-01-01T00:00:00+00:00",
        disconnected_at=None,
    )
