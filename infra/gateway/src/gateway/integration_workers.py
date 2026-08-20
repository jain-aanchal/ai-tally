"""Shared plumbing for the third-party ingest workers (CTO-127).

Segment, HubSpot and Pendo each run as a per-tenant *cycle*: resolve the tenant's credential (by
reference), fetch a batch of events from the third party over an **injected** HTTP client (never a
live network call in tests), map them onto ``business_events`` / ``identity_graph`` rows, and stamp
the outcome via :meth:`TenantIntegrationStore.record_run`.

This module holds what all three share:

* :class:`HttpClient` — the injectable transport Protocol (one ``get_json`` method).
* :class:`IngestWorker` — the base that owns credential resolution, the ClickHouse writes' error
  boundary, and the ``record_run`` bookkeeping (with PII-scrubbed errors and honest
  success / partial / failed / skipped status), so each connector only describes its own fetch +
  mapping in :meth:`IngestWorker._ingest`.

Invariants enforced here (not left to each connector): no raw message bodies are ever persisted
(only counts / mapped events / values), credentials are held only transiently as a resolved token,
and every error string routed to storage goes through :func:`scrub_error_message` first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Protocol

from tally.hmac_keys import HmacKeyRegistry
from tally.wire import BusinessEvent, IdentityLink

from gateway.store import ClickHouseStore
from gateway.tenant_integration_secrets import (
    IntegrationSecret,
    SecretResolver,
    TenantIntegrationSecretStore,
)
from gateway.tenant_integrations import RunStatus, TenantIntegrationStore, scrub_error_message

logger = logging.getLogger("tally.gateway.integrations")


class HttpClient(Protocol):
    """Minimal injectable HTTP transport. Implementations parse and return JSON.

    Kept deliberately tiny so tests supply a canned-response / raising fake and never touch the
    network. A real implementation (urllib / httpx) raises on a non-2xx status so the worker's
    error boundary records an honest ``failed`` run.
    """

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What one connector's :meth:`IngestWorker._ingest` produced for the cycle."""

    event_count: int
    partial: bool = False
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CycleResult:
    """The outcome of one worker cycle, as returned to a scheduler / caller.

    ``status`` is one of ``success`` / ``partial`` / ``failed`` (mirroring ``record_run``) plus
    ``skipped`` when the tenant has not connected this integration (nothing to do, nothing
    recorded). ``recorded`` is False when ``record_run`` itself failed — a best-effort write that
    never propagates, so a control-plane blip can't crash the cycle.
    """

    connector_id: str
    status: str
    event_count: int = 0
    error_message: str | None = None
    recorded: bool = False


# --- time / value helpers (shared by the mappers) -----------------------------------------------


def ms_to_ns(ms: Any) -> int:
    """Epoch-milliseconds → epoch-nanoseconds. Falls back to now() on a bad value."""
    try:
        return int(ms) * 1_000_000
    except (TypeError, ValueError):
        return time.time_ns()


def iso_to_ns(ts: Any) -> int:
    """ISO-8601 timestamp → epoch-nanoseconds. Falls back to now() on a missing / bad value."""
    if not isinstance(ts, str) or not ts.strip():
        return time.time_ns()
    s = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return time.time_ns()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def dollars_to_micro(amount: Any) -> int | None:
    """Currency units (dollars) → integer micro-USD. ``None`` in → ``None`` out (no fabrication)."""
    if amount is None:
        return None
    try:
        return int(round(float(amount) * 1_000_000))
    except (TypeError, ValueError):
        return None


def as_event_list(payload: Any) -> list[Any]:
    """Coax a fetched payload into a list of raw event objects.

    Accepts a bare list or the common ``{"events"|"results"|"data": [...]}`` envelope shapes.
    Anything else yields an empty list — an unrecognized shape is zero events, not a crash.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def build_hasher(
    registry: HmacKeyRegistry, tenant_id: str
) -> Callable[[str | None], str]:
    """Return a per-tenant identifier hasher: ``str -> 64-hex HMAC`` (``""`` for empty input).

    Provisions the tenant's key once up front. Unlike the Stripe email hasher this does *not*
    lowercase — a track/visitor id is case-sensitive; callers that hash an email lowercase it
    themselves before calling.
    """
    registry.provision(tenant_id)

    def _hash(value: str | None) -> str:
        if not value:
            return ""
        return registry.hash(tenant_id, value.strip()).value

    return _hash


# --- base worker --------------------------------------------------------------------------------


class IngestWorker:
    """Base for the Segment / HubSpot / Pendo workers.

    Subclasses set :attr:`connector_id` and implement :meth:`_ingest`. Everything else — credential
    resolution, the ``record_run`` write with a scrubbed error, and the honest status derivation —
    lives here so the connectors stay small and consistent.
    """

    connector_id: ClassVar[str] = ""

    def __init__(
        self,
        *,
        secrets: TenantIntegrationSecretStore,
        resolver: SecretResolver,
        http: HttpClient,
        store: ClickHouseStore,
        integrations: TenantIntegrationStore,
        registry: HmacKeyRegistry,
    ) -> None:
        self._secrets = secrets
        self._resolver = resolver
        self._http = http
        self._store = store
        self._integrations = integrations
        self._registry = registry

    def run_cycle(self, tenant_id: str) -> CycleResult:
        """Run one ingest cycle for one tenant. Never raises — every failure is recorded, not thrown.

        Skips silently (no ``record_run``) when the tenant hasn't connected this integration, since
        an absent row is the honest "not connected" state the dashboard already renders.
        """
        secret = self._secrets.get(tenant_id, self.connector_id)
        if secret is None or not secret.is_active:
            return CycleResult(self.connector_id, "skipped")

        try:
            token = self._resolver.resolve(secret.secret_ref)
        except Exception as exc:  # noqa: BLE001 — any resolver failure is an honest 'failed' cycle
            return self._finish(tenant_id, "failed", 0, f"credential resolution failed: {exc}")

        try:
            outcome = self._ingest(tenant_id, secret, token)
        except Exception as exc:  # noqa: BLE001 — a fetch / insert failure is a 'failed' cycle
            return self._finish(tenant_id, "failed", 0, str(exc))

        status: RunStatus = "partial" if outcome.partial else "success"
        return self._finish(tenant_id, status, outcome.event_count, outcome.error_message)

    def _ingest(
        self, tenant_id: str, secret: IntegrationSecret, token: str
    ) -> IngestOutcome:  # pragma: no cover - abstract
        raise NotImplementedError

    def _finish(
        self,
        tenant_id: str,
        status: RunStatus,
        count: int,
        error: str | None,
    ) -> CycleResult:
        # No raw PII may reach the returned CycleResult, so scrub it here with the shared util.
        # record_run applies the *same* util before it persists, so we hand it the raw message and
        # let it scrub exactly once — the scrub isn't idempotent (its own "[redacted-email]" marker
        # contains the forbidden substring "email"), so a pre-scrubbed value would over-collapse.
        scrubbed = scrub_error_message(error)
        recorded = False
        try:
            self._integrations.record_run(
                tenant_id,
                self.connector_id,
                status,
                event_count=count,
                error_message=error,
            )
            recorded = True
        except Exception:  # noqa: BLE001 — best-effort: a control-plane blip can't crash the cycle
            logger.exception(
                "record_run failed for tenant %s connector %s", tenant_id, self.connector_id
            )
        return CycleResult(self.connector_id, status, count, scrubbed, recorded)

    # --- shared write helpers used by subclasses ---

    def _insert_events(self, tenant_id: str, events: list[BusinessEvent]) -> int:
        return self._store.insert_business_events(tenant_id, events)

    def _insert_links(self, tenant_id: str, links: list[IdentityLink]) -> int:
        return self._store.insert_identity_links(tenant_id, links)
