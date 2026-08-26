"""Shared plumbing for the third-party ingest workers (CTO-127).

Segment, HubSpot and Pendo each run as a per-tenant *cycle*: resolve the tenant's credential (by
reference), fetch a batch of events from the third party over an **injected** HTTP client (never a
live network call in tests), map them onto ``business_events`` / ``identity_graph`` rows, and stamp
the outcome via :meth:`TenantIntegrationStore.record_run`.

This module holds what all three share:

* :class:`HttpClient` — the injectable transport Protocol (one ``get_json`` method).
* :class:`IngestWorker`, the base that owns credential resolution, the incremental window, the
  ClickHouse writes' error boundary, and the ``record_run`` bookkeeping (with PII-scrubbed errors
  and honest success / partial / failed / skipped status), so each connector only describes its own
  fetch + mapping in :meth:`IngestWorker._ingest`.
* :data:`CONNECTOR_TIMEOUT_S`, the per-provider HTTP timeout. One global default cannot be right
  for both Segment (answers in milliseconds) and Pendo (documents a five-minute query timeout);
  CTO-219 replaced it with a number per provider.
* :class:`IngestWindow`, the ``[since, until]`` range one cycle asks for, derived from the
  per-tenant watermark in :mod:`gateway.ingest_cursors` (CTO-219). Before that every cycle re-pulled
  the provider's whole default window, 96 times a day per tenant per connector.

Invariants enforced here (not left to each connector): no raw message bodies are ever persisted
(only counts / mapped events / values), credentials are held only transiently as a resolved token,
and every error string routed to storage goes through :func:`scrub_error_message` first.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tally.account_identity import AccountLinker
from tally.hmac_keys import HmacKeyRegistry
from tally.wire import BusinessEvent, IdentityLink

from gateway.ingest_cursors import (
    INITIAL_LOOKBACK_S,
    CursorStore,
    NullCursorStore,
    next_cursor,
)
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


class UrllibHttpClient:
    """The production :class:`HttpClient`. stdlib ``urllib`` only, so the base install stays slim.

    WHY this exists (CTO-216). The Protocol above has always had exactly one implementation, the
    test fake, because nothing ever ran a cycle outside a test. Scheduling the workers is what makes
    a real transport necessary, and the docstring on :class:`HttpClient` already named
    ``urllib / httpx`` as the intended shape. ``urllib`` wins because ``requests`` is not a declared
    dependency of this package (the Vercel and egress connectors lazy-import it), and one GET every
    few minutes does not justify adding one.

    Raises on any non-2xx, which is the contract :meth:`IngestWorker.run_cycle` relies on to record
    an honest ``failed`` run. The status line, not the body, goes into the exception: a third-party
    error body is exactly where a customer email turns up, and while ``scrub_error_message`` would
    catch it on the way to storage, not putting it in the string is the cheaper guarantee.

    ``timeout_s`` has NO default (CTO-219). It used to default to 30 seconds for all three
    providers, which is a number that cannot be right for all three: see
    :data:`CONNECTOR_TIMEOUT_S`. Making it required means a caller has to say which provider's
    budget it is spending.
    """

    def __init__(self, *, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._timeout_s = float(timeout_s)

    @property
    def timeout_s(self) -> float:
        """The socket timeout this client enforces, in seconds. Read-only; set at construction."""
        return self._timeout_s

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        target = url
        if params:
            target = f"{url}{'&' if urlsplit(url).query else '?'}{urlencode(dict(params))}"
        request = Request(target, headers=dict(headers or {}), method="GET")  # noqa: S310
        try:
            with urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                payload = response.read()
        except HTTPError as exc:  # non-2xx: surface the code, never the body
            raise RuntimeError(f"HTTP {exc.code} from {urlsplit(url).netloc}") from None
        except URLError as exc:
            raise RuntimeError(f"could not reach {urlsplit(url).netloc}: {exc.reason}") from None
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))


# --- per-integration HTTP timeouts (CTO-219) ------------------------------------------------------
#
# One global 30-second default used to serve all three providers, and it directly contradicted the
# cadence argument in gateway.worker_jobs: that comment justifies Pendo's 30-minute cadence partly by
# noting Pendo's aggregation API has a DOCUMENTED FIVE-MINUTE query timeout and returns responses up
# to 4GB. A 30-second client timeout means every Pendo tenant whose aggregation takes longer than 30
# seconds fails on every single cycle, records `failed`, and is pushed into the scheduler's
# exponential backoff until it is retrying at the 6-hour cap. The integration is then effectively
# dead for exactly the tenants with enough data to be slow, which is to say the ones it matters for.
#
# The fix is NOT to raise the global number. A 5-minute timeout on Segment would hold a worker thread
# on a hung connection for five minutes, and the scheduler runs every job body through
# `asyncio.to_thread` in the gateway's own process, so those threads are not free. Each provider gets
# the budget its own documented behaviour justifies:
#
#   segment   20s. Segment's Public API is a paged event read with per-minute rate limits, i.e. it is
#             built to answer fast. Anything slower than 20s is a stuck connection, not a slow query,
#             and the next cycle is 15 minutes away.
#   hubspot   30s. Unchanged, and the one provider the old global default actually suited. HubSpot
#             publishes burst limits in requests per 10 seconds, so it too is built to answer fast;
#             30s leaves room for a large page without holding a thread pointlessly.
#   pendo    330s. Pendo's own documented query timeout is 300s, so a client timeout at or below that
#             makes US the thing that fails rather than Pendo, and we cannot tell a slow query from a
#             broken one. 330s is that documented ceiling plus 30s of transfer slack for a large
#             response body. It is affordable only because Pendo's cadence is 30 minutes: one thread
#             held for at most 5.5 minutes out of every 30 per tenant.
CONNECTOR_TIMEOUT_S: dict[str, float] = {
    "segment": 20.0,
    "hubspot": 30.0,
    "pendo": 330.0,
}

#: Used for a connector with no entry above. Deliberately tight rather than generous: a provider
#: nobody has sized yet should hold a worker thread for as little as possible.
DEFAULT_CONNECTOR_TIMEOUT_S = 30.0


def timeout_for(connector_id: str) -> float:
    """This connector's HTTP timeout in seconds. See :data:`CONNECTOR_TIMEOUT_S`."""
    return CONNECTOR_TIMEOUT_S.get(connector_id, DEFAULT_CONNECTOR_TIMEOUT_S)


def build_http_client(connector_id: str) -> UrllibHttpClient:
    """A production HTTP client sized for one connector.

    One client PER connector, not one shared client, because the timeout is the thing that differs
    and it is a property of the transport. Sharing one would put every provider back on a single
    number, which is the bug.
    """
    return UrllibHttpClient(timeout_s=timeout_for(connector_id))


@dataclass(frozen=True, slots=True)
class IngestWindow:
    """The half-open-ish time range one cycle asks the provider for: ``[since, until]``.

    Handed to :meth:`IngestWorker._ingest` so each connector can translate it into whatever its own
    API calls those parameters. ``is_first_run`` is carried explicitly rather than inferred from the
    width, so a connector (or a test, or a log line) can tell "this tenant has never run" from "this
    tenant was down for a week", which look identical from the timestamps alone.
    """

    since: datetime
    until: datetime
    is_first_run: bool = False

    @property
    def span_seconds(self) -> float:
        return (self.until - self.since).total_seconds()

    def since_ms(self) -> int:
        """``since`` as epoch milliseconds, for the providers whose APIs take ms (HubSpot, Pendo)."""
        return int(self.since.timestamp() * 1000)

    def until_ms(self) -> int:
        return int(self.until.timestamp() * 1000)

    def since_iso(self) -> str:
        """``since`` as an ISO-8601 UTC instant, for the providers whose APIs take one (Segment)."""
        return self.since.astimezone(timezone.utc).isoformat()

    def until_iso(self) -> str:
        return self.until.astimezone(timezone.utc).isoformat()


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
        account_linker: AccountLinker | None = None,
        cursors: CursorStore | None = None,
    ) -> None:
        self._secrets = secrets
        self._resolver = resolver
        self._http = http
        self._store = store
        self._integrations = integrations
        self._registry = registry
        # CTO-219: the incremental watermark. Optional and defaulted so every existing construction
        # (and every mapper unit test) keeps working; a worker without one re-pulls the initial
        # window every cycle, which is the pre-CTO-219 behaviour. The production wiring in
        # gateway.worker_jobs always passes a real IngestCursorStore.
        self._cursors: CursorStore = cursors if cursors is not None else NullCursorStore()
        # CTO-195: shared user→account map, so a connector can fill in an account for a revenue
        # event that names a person but no company. Optional and defaulted so every existing
        # construction keeps working; a worker with its own linker simply learns nothing from the
        # others, which costs an honest blank rather than a wrong account.
        self._account_linker = account_linker if account_linker is not None else AccountLinker()

    def run_cycle(self, tenant_id: str) -> CycleResult:
        """Run one ingest cycle for one tenant. Never raises — every failure is recorded, not thrown.

        Skips silently (no ``record_run``) when the tenant hasn't connected this integration, since
        an absent row is the honest "not connected" state the dashboard already renders.

        INCREMENTAL since CTO-219. The cycle asks the provider only for ``[cursor, now]`` and then
        advances the cursor, so a 15-minute cadence stops re-pulling the provider's whole window 96
        times a day. See :mod:`gateway.ingest_cursors` for the window, the per-connector overlap and
        an accurate account of what re-pulling actually cost (write amplification and a transient
        pre-merge double count, NOT corruption: re-insertion is idempotent by design).
        """
        secret = self._secrets.get(tenant_id, self.connector_id)
        if secret is None or not secret.is_active:
            return CycleResult(self.connector_id, "skipped")

        try:
            token = self._resolver.resolve(secret.secret_ref)
        except Exception as exc:  # noqa: BLE001 — any resolver failure is an honest 'failed' cycle
            return self._finish(tenant_id, "failed", 0, f"credential resolution failed: {exc}")

        window = self._window(tenant_id)
        try:
            outcome = self._ingest(tenant_id, secret, token, window)
        except Exception as exc:  # noqa: BLE001 — a fetch / insert failure is a 'failed' cycle
            # NO cursor advance on a failed cycle. The window has not been handled, so the next
            # cycle must ask for it again; advancing here would silently drop every event in it.
            return self._finish(tenant_id, "failed", 0, str(exc))

        status: RunStatus = "partial" if outcome.partial else "success"
        self._advance_cursor(tenant_id, window)
        return self._finish(tenant_id, status, outcome.event_count, outcome.error_message)

    def _window(self, tenant_id: str) -> IngestWindow:
        """The time range this cycle asks the provider for.

        A cursor read that FAILS is not a failed cycle: it falls back to the initial window, which
        re-pulls more than necessary and is idempotent, rather than skipping data or aborting. The
        control plane being briefly unreachable should cost bandwidth, not events.
        """
        until = datetime.now(tz=timezone.utc)
        cursor: datetime | None = None
        try:
            cursor = self._cursors.get(tenant_id, self.connector_id)
        except Exception:  # noqa: BLE001 - see docstring: degrade to a wider window, never skip
            logger.exception(
                "cursor read failed for tenant %s connector %s; using the initial window",
                tenant_id,
                self.connector_id,
            )
        if cursor is None:
            return IngestWindow(
                since=until - timedelta(seconds=INITIAL_LOOKBACK_S),
                until=until,
                is_first_run=True,
            )
        # A cursor from the future (clock skew, or a replica that ran ahead) would produce an
        # inverted window that some providers answer with an error and others with everything.
        # Clamp it to an empty-but-valid window instead.
        return IngestWindow(since=min(cursor, until), until=until)

    def _advance_cursor(self, tenant_id: str, window: IngestWindow) -> None:
        """Move the watermark to the end of the handled window, less this connector's overlap.

        Called on ``success`` AND on ``partial``. A partial cycle is one where some records failed
        to MAP, which is deterministic: the same bytes will fail the same way next cycle, so holding
        the cursor back would re-pull them forever and never make progress. The failure itself is
        not lost: ``record_run`` has the ``partial`` status and the reason.

        Best-effort, like ``record_run``: a control-plane blip must not turn a cycle that already
        wrote its data into a failure. The cost of a dropped advance is one repeated window, which
        is idempotent.
        """
        try:
            self._cursors.advance(
                tenant_id, self.connector_id, next_cursor(self.connector_id, window.until)
            )
        except Exception:  # noqa: BLE001 - best-effort: a dropped advance costs a repeat, not data
            logger.exception(
                "cursor advance failed for tenant %s connector %s", tenant_id, self.connector_id
            )

    def _ingest(
        self, tenant_id: str, secret: IntegrationSecret, token: str, window: IngestWindow
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
