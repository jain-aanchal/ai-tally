# SPDX-License-Identifier: Apache-2.0
"""Scheduled jobs for the third-party ingest workers and the reconciler (CTO-216, S4).

WHY this exists. Two pieces of working, tested code in this repo were invoked by nobody.
:meth:`gateway.integration_workers.IngestWorker.run_cycle` backs Segment, HubSpot and Pendo and had
no caller; :func:`gateway.reconciliation.run_reconciliation` had no caller either, which is why the
/features attribution diagnostics card reads "last reconciled" from a run that never happened. The
scheduler engine (CTO-213) and its multi-replica locking (CTO-214) landed with an empty registry
precisely so that a ticket like this one could fill it. This module is the filling: it builds the
workers and turns each one into a :class:`~gateway.scheduler.Job`.

WHAT THIS DOES NOT FIX. ``attribution_records`` stays empty and /features keeps showing honest nulls
for value, payback and attribution rate. That needs the stitcher RUNNER, which does not exist at all
(CTO-200): ``sdk/python/src/tally/stitcher.py`` is a pure in-memory library with no ClickHouse-backed
``TouchStore`` and no writer, so there is nothing here to schedule. Scheduling the reconciler is a
different thing from scheduling the stitcher, and nothing in this module should be read as the
latter.

STATUS MAPPING, and specifically ``partial``. ``run_cycle`` already records its own outcome through
:meth:`gateway.tenant_integrations.TenantIntegrationStore.record_run`, with PII-scrubbed errors and
an honest ``success`` / ``partial`` / ``failed`` / ``skipped`` status. That is the source of truth
for "how is this tenant's Segment connector doing", it is what the dashboard already reads, and this
module does not duplicate it. What the job body does is translate that outcome into the scheduler's
own three-value vocabulary, which exists to answer a different question ("should this run again, and
when"):

* ``skipped``  -> :class:`~gateway.scheduler.JobSkipped`. The tenant has not connected this
  integration, which is most tenants for most connectors. It settles the cadence window so they are
  not re-asked every tick, and it is not a success, so a freshness surface cannot mistake it for
  current data.
* ``success``  -> return normally.
* ``partial``  -> ALSO return normally, i.e. scheduler ``success``. The scheduler has no ``partial``
  and should not grow one. A partial cycle DID write data, so the cadence window is genuinely
  satisfied and the next run belongs one interval away, not immediately. Mapping it to ``failed``
  would be worse in both directions: it would put a chronically-partial connector (one malformed
  record per batch is enough) into exponential backoff and starve the tenant of the good data it
  does get, and it would make the pair look broken in the run history when it is not. The
  partial-ness is not lost by this: ``record_run`` has already written ``partial`` and the reason
  onto the ``tenant_integration_runs`` row that the integrations status surface reads, which is the
  surface that is actually about connector health.
* ``failed``   -> raise, so the scheduler records ``failed`` and applies its backoff.

Cadences are per job and defended below, at the constants.

CTO-219 REVIEW FIXES. Three things this module wires up were wrong in ways the cadences above hid:

* The HTTP timeout was one global 30 seconds, which contradicted the Pendo cadence argument below in
  its own terms (that argument leans on Pendo's documented 5-minute aggregation). Timeouts are now
  per connector, sized to what each provider actually does. See
  :data:`gateway.integration_workers.CONNECTOR_TIMEOUT_S`.
* Ingest had no cursor, so each cycle re-pulled the provider's whole default payload. It does not
  any more: see :mod:`gateway.ingest_cursors`. Be accurate about what that cost, because the first
  write-up was not: re-insertion was already idempotent (``BusinessEventId`` is the provider's stable
  id and ``business_events`` is a ``ReplacingMergeTree`` ordered on it), so the costs were write
  amplification and a transient pre-merge double count on the reads that lack ``FINAL``, not
  corruption.
* The reconciler's ClickHouse scan was bounded only in rows RETURNED, so its ASOF join read an
  uncapped 14 days of ``otel_spans`` every hour and blew up on the biggest tenants. See
  :class:`gateway.reconciliation.ClickHouseLateArrivalSource`.

None of that changes what is said above about /features, which stays blocked on CTO-200.
"""

from __future__ import annotations

import logging

from tally.account_identity import AccountLinker
from tally.hmac_keys import HmacKeyRegistry

from gateway.config import Settings
from gateway.hubspot_ingest import HubSpotWorker
from gateway.ingest_cursors import CursorStore, IngestCursorStore
from gateway.integration_workers import HttpClient, IngestWorker, build_http_client
from gateway.pendo_ingest import PendoWorker
from gateway.reconciliation import (
    ClickHouseLateArrivalSource,
    ReconciliationStore,
    run_reconciliation,
)
from gateway.scheduler import Job, JobFn, JobRegistry, JobSkipped
from gateway.segment_ingest import SegmentWorker
from gateway.store import ClickHouseStore
from gateway.tenant_integration_secrets import (
    EnvSecretResolver,
    SecretResolver,
    TenantIntegrationSecretStore,
)
from gateway.tenant_integrations import TenantIntegrationStore

logger = logging.getLogger("tally.gateway.worker_jobs")

# --- cadences ------------------------------------------------------------------------------------
#
# These are not daily. The dashboard reads what these workers write, so a daily cycle would mean a
# conversion recorded this morning does not appear until tomorrow, and the number a customer is
# looking at would be up to 24 hours old with nothing on the page admitting it. Against that,
# every cycle is a call to somebody else's rate-limited API, so "as often as possible" is not the
# answer either. Each of these is one GET per tenant per cycle.

#: Segment and HubSpot: every 15 minutes. 96 cycles per tenant per day, per connector.
#:
#: HubSpot documents the tightest published ceilings of the three and 15 minutes clears them by
#: orders of magnitude: the burst limit is 100 requests per 10 seconds (190 on Professional and
#: Enterprise) and the daily cap is 250,000 requests (625,000 / 1,000,000 on the paid tiers), shared
#: across every app on the account. At 96 calls a day we would need well over two thousand tenants
#: on one HubSpot account before the daily cap was even in view, and the burst limit is unreachable
#: by construction because the tick loop walks tenants sequentially.
#:
#: Segment's Public API publishes per-endpoint limits in requests per MINUTE, the tightest of the
#: documented ones being 25/min. One call per tenant per 15 minutes is nowhere near it.
INGEST_INTERVAL_S = 15 * 60.0

#: Pendo: every 30 minutes. Deliberately half the frequency of the other two, for one reason: Pendo
#: does not publish numeric rate limits at all, so there is no ceiling to check ourselves against
#: and the honest posture is caution. Its aggregation API is also the heavyweight of the three
#: (documented 5-minute query timeout, responses up to 4GB), so a Pendo cycle costs the provider
#: materially more than a Segment or HubSpot pull. 30 minutes still keeps the dashboard inside a
#: window a human would call current, and can be tightened later if a real published limit appears.
#:
#: The HTTP TIMEOUT has to agree with that argument, and until CTO-219 it did not: one global
#: 30-second default meant a Pendo aggregation that took the 5 minutes Pendo itself documents was
#: cut off by us on every cycle, recorded `failed`, and pushed into the scheduler's backoff until it
#: was retrying at the 6-hour cap. Timeouts are per connector now
#: (:data:`gateway.integration_workers.CONNECTOR_TIMEOUT_S`), and Pendo's 330s budget is affordable
#: precisely BECAUSE of this cadence: at most 5.5 minutes of a worker thread out of every 30.
PENDO_INGEST_INTERVAL_S = 30 * 60.0

#: The reconciler: hourly. It hits our own ClickHouse rather than anyone else's API, so third-party
#: limits do not apply, but the scan is a join over a tenant's recent events and spans and there is
#: no point paying for it every few minutes. What it feeds is the /features diagnostics card, whose
#: whole job is to say how stale the picture is; an hour is fine granularity for that, and it lines
#: up with the one-hour lateness threshold the card is documented against, so a pass can never be
#: more than one threshold-width behind the events it is measuring.
RECONCILIATION_INTERVAL_S = 3600.0

INGEST_JOB_NAMES: dict[str, str] = {
    "segment": "ingest.segment",
    "hubspot": "ingest.hubspot",
    "pendo": "ingest.pendo",
}
RECONCILIATION_JOB_NAME = "reconciliation"


class IngestCycleFailed(RuntimeError):
    """A cycle that :meth:`IngestWorker.run_cycle` reported as ``failed``.

    Raised purely so the scheduler sees a failure and applies its backoff. It carries no state the
    ``tenant_integration_runs`` row does not already have.
    """


class ReconciliationFailed(RuntimeError):
    """A reconciliation pass whose ClickHouse scan failed. Same purpose as :class:`IngestCycleFailed`."""


def _ingest_job_body(worker: IngestWorker) -> JobFn:
    """Wrap one worker as a scheduler job body: tenant id in, nothing out, exceptions for status."""

    def _run(tenant_id: str) -> None:
        result = worker.run_cycle(tenant_id)
        if result.status == "skipped":
            # The tenant has not connected this integration. Not an error, and not a success.
            raise JobSkipped(f"{worker.connector_id} not connected for tenant")
        if result.status == "failed":
            # record_run already holds the scrubbed reason (unless it too failed, hence
            # `recorded`). The scheduler scrubs this string again on its way to scheduler_runs, and
            # a message that already had an address redacted collapses to the coarse
            # "[redacted: contained PII key]" marker on that second pass. That is acceptable: it is
            # coarser, never leakier, and the precise reason is on the tenant_integration_runs row.
            raise IngestCycleFailed(
                f"{worker.connector_id} cycle failed"
                f" (recorded={result.recorded}): {result.error_message or 'no detail reported'}"
            )
        # success or partial. See the module docstring for why partial lands here.
        logger.info(
            "ingest cycle: connector=%s tenant=%s status=%s events=%d",
            worker.connector_id,
            tenant_id,
            result.status,
            result.event_count,
        )

    return _run


def _reconciliation_job_body(
    source: ClickHouseLateArrivalSource, store: ReconciliationStore
) -> JobFn:
    """Wrap one reconciliation pass as a scheduler job body.

    :func:`run_reconciliation` never raises: a failed CH scan is recorded as a ``failed`` run with
    zeroed metrics, so the card can say "ran, but errored" instead of silently going stale. That is
    the right behaviour for the reconciler's own log and the wrong signal for the scheduler, which
    would otherwise read the pass as a success, settle the window and retry a broken scan on the
    full cadence forever. So the status is re-read and re-raised here.

    No :class:`JobSkipped` path. Every tenant is in scope for the reconciler: there is nothing to
    configure, and a tenant with no events yields an honest zero rather than "nothing to do".
    """

    def _run(tenant_id: str) -> None:
        run = run_reconciliation(source, store, tenant_id)
        if run.status == "failed":
            raise ReconciliationFailed("reconciliation scan failed; see reconciliation_runs")
        logger.info(
            "reconciliation: tenant=%s status=%s late=%d median_lag_s=%d",
            tenant_id,
            run.status,
            run.events_late,
            run.lag_seconds_median,
        )

    return _run


def register_worker_jobs(
    registry: JobRegistry,
    settings: Settings,
    *,
    store: ClickHouseStore,
    hmac_registry: HmacKeyRegistry,
    account_linker: AccountLinker | None = None,
    secrets: TenantIntegrationSecretStore | None = None,
    resolver: SecretResolver | None = None,
    http: HttpClient | None = None,
    integrations: TenantIntegrationStore | None = None,
    reconciliation_store: ReconciliationStore | None = None,
    cursors: CursorStore | None = None,
) -> tuple[Job, ...]:
    """Register the three ingest jobs and the reconciler on ``registry``. Returns what it added.

    Every collaborator is injectable and defaults to the production one built from ``settings``,
    which is the same shape :func:`gateway.scheduler.build_scheduler` uses, and is what lets the
    tests drive a full tick with fakes and no Postgres, ClickHouse or network.

    ``hmac_registry`` and ``account_linker`` are passed in rather than constructed because they hold
    process-local state that must be SHARED with the request path: the HMAC registry provisions the
    per-tenant key that decides which ``UserIdHash`` space a worker writes into, and the account
    linker learns user-to-account edges from every connector in the process. A second instance of
    either would not error, it would quietly write into a different hash space or fail to learn, and
    the symptom would be an attribution join that lights up for some rows and not others.
    """
    ch_secrets = secrets if secrets is not None else TenantIntegrationSecretStore(settings)
    ch_resolver = resolver if resolver is not None else EnvSecretResolver()
    ch_integrations = integrations if integrations is not None else TenantIntegrationStore(settings)
    ch_cursors = cursors if cursors is not None else IngestCursorStore(settings)
    recon_store = (
        reconciliation_store if reconciliation_store is not None else ReconciliationStore(settings)
    )

    common = {
        "secrets": ch_secrets,
        "resolver": ch_resolver,
        "store": store,
        "integrations": ch_integrations,
        "registry": hmac_registry,
        "account_linker": account_linker,
        # CTO-219: the incremental watermark, so a 15-minute cadence stops re-pulling the
        # provider's whole window 96 times a day. See gateway.ingest_cursors.
        "cursors": ch_cursors,
    }

    def _http_for(connector_id: str) -> HttpClient:
        """This connector's transport. An injected ``http`` overrides all three (tests do this).

        CTO-219: otherwise ONE CLIENT PER CONNECTOR, because the socket timeout differs per provider
        and is a property of the transport. A single shared client would put Segment, HubSpot and
        Pendo back on one number, and there is no one number that is right: see
        :data:`gateway.integration_workers.CONNECTOR_TIMEOUT_S` and the Pendo cadence note above.
        """
        return http if http is not None else build_http_client(connector_id)

    workers: tuple[tuple[IngestWorker, float], ...] = (
        (SegmentWorker(http=_http_for("segment"), **common), INGEST_INTERVAL_S),  # type: ignore[arg-type]
        (HubSpotWorker(http=_http_for("hubspot"), **common), INGEST_INTERVAL_S),  # type: ignore[arg-type]
        (PendoWorker(http=_http_for("pendo"), **common), PENDO_INGEST_INTERVAL_S),  # type: ignore[arg-type]
    )

    added: list[Job] = []
    for worker, interval_s in workers:
        added.append(
            registry.register(
                INGEST_JOB_NAMES[worker.connector_id],
                interval_s,
                _ingest_job_body(worker),
            )
        )
    added.append(
        registry.register(
            RECONCILIATION_JOB_NAME,
            RECONCILIATION_INTERVAL_S,
            _reconciliation_job_body(ClickHouseLateArrivalSource(store), recon_store),
        )
    )
    return tuple(added)


__all__ = [
    "INGEST_INTERVAL_S",
    "INGEST_JOB_NAMES",
    "PENDO_INGEST_INTERVAL_S",
    "RECONCILIATION_INTERVAL_S",
    "RECONCILIATION_JOB_NAME",
    "IngestCycleFailed",
    "ReconciliationFailed",
    "register_worker_jobs",
]
