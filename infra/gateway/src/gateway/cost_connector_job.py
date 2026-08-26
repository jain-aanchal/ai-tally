# SPDX-License-Identifier: Apache-2.0
"""The daily cloud cost-connector job (CTO-215, S3): the thing that finally CALLS the connectors.

WHY this exists. ``ComputeCostConnector``, ``EgressCostConnector`` and ``VercelCostConnector`` all
shipped with a working ``run()`` and ``run_backfill()``, and the only callers were
``scripts/backfill_*.py``, invoked by a human. CTO-176 then shipped the Connect flow, so a tenant can
configure AWS, GCP, Vercel or Cloudflare from ``/connectors``, and that config was never acted on: a
customer connected a cloud account and nothing happened, forever. This module is the job body the
scheduler (CTO-213) runs once a day per tenant, and it is what makes the Compute and Egress columns
populate on their own.

It is deliberately NOT a new connector. Everything here is orchestration: read the config rows that
CTO-176 writes, construct the connectors that already exist, run them for the days that are owed,
and let them record their own outcome.

Run recording has ONE owner per fact
------------------------------------
The connectors already stamp ``last_run_at`` / ``last_status`` on their own config row through the
``RunRecorder`` protocol, and this job passes those same recorders in. There is no second "did the
connector run" store here, because two sources of truth for one fact is how a dashboard ends up
disagreeing with itself. The one wrapper in this module, :class:`_WorstOfRecorder`, does not become
a second store: it forwards to the connector's own recorder and only decides WHICH of two sub-run
outcomes that one row ends up carrying (see "One shared config row, one status"). The division is:

* ``scheduler_runs`` answers "did the SCHEDULE fire for this tenant" (owned by the scheduler).
* ``tenant_*_config.last_status`` answers "did the CONNECTOR work" (owned by the connector).

An unconfigured tenant raises :class:`gateway.scheduler.JobSkipped` rather than returning success.
Most tenants have no cloud account connected, and a ``skipped`` row settles the cadence window (so
they are not re-asked every tick) while staying distinguishable from a real run, so a freshness
surface can never read "we ran, all good" off a tenant we never had anything to do for.

Which days
----------
Yesterday UTC is the NEWEST billable day (:func:`target_day`). Billing APIs only report a complete
day once it has closed, so asking for today would fetch a partial number. That partial number would
then be FROZEN: the emitter is keyed on ``(tenant, provider, operation, day)`` and skips a day that
already has a span, so nothing would ever correct it. One day of lag is the honest choice, and it is
the same window the backfill scripts default to.

The OLDEST day of a run is derived from the last day actually billed, not from today's date
(CTO-219, finding 1). CTO-215 said "run for yesterday UTC" and that is exactly what shipped, so any
run gap spanning two UTC midnights lost a day of cloud spend permanently: the missed day was never
asked for again, and because the emitter skips a day that already has a span it also could not be
picked up later by widening some other run. Cadence drift towards midnight makes that routine rather
than exotic. So the job now bills ``[last billed day + 1, yesterday]`` and catches the gap up.

Where "the last day actually billed" comes from: the spans themselves. For each
``(provider, operation)`` series the job walks back from yesterday probing the SAME deterministic
:func:`gateway.connectors.base.synthetic_span_id` the emitter's idempotency guard checks, and the
newest day that has a span is the last day billed. That is authoritative because the span IS the
billing output for a day, it is cheap (tenant-scoped, bloom-filtered ``SpanId`` point lookups, the
query the connectors already run before every insert), and it introduces no second source of truth.
The alternative, ``tenant_*_config.last_run_at``, answers a different question: it says when a RUN
happened, not which DAY was billed, so a run that failed, was skipped by the gate, or covered a
different window would all read identically.

Both ends of the window are bounded:

* :data:`MAX_CATCHUP_DAYS` caps ONE invocation at a week of billing days. A connector broken for
  three months must not try to bill ninety days in one run and time out. The remainder is not lost:
  the next run reads the new last-billed day and continues from there, so a long gap closes over
  consecutive runs rather than in one heroic one.
* :data:`LOOKBACK_DAYS` caps the SEARCH at a fortnight of probes. If nothing was billed inside that
  window the job bills yesterday alone and starts a fresh chain from it. That is the right answer
  for the two cases that look identical from here: a brand new connector, which has no history and
  should not quietly backfill one, and a long dead one, whose deep history belongs to
  ``scripts/backfill_*.py`` (which ``docs/scheduler-scope.md`` deliberately keeps for exactly this).

A multi-day window goes through ``run_backfill(config, start_day=..., end_day=...)``, which every
connector already exposes and which fetches the whole range in ONE billing-API call, rather than
looping ``run()`` once per day.

One shared config row, one status
---------------------------------
``VercelCostConnector`` has two sub-runs (compute always, egress when the CTO-163 gate is on) and
both stamp the SAME ``tenant_vercel_config`` row, so the row used to show whatever finished last: a
failed compute sub-run followed by a successful egress sub-run left ``last_status = 'success'`` on a
customer-facing row while no compute span had been emitted (CTO-219, finding 2). The Vercel recorder
is therefore wrapped in :class:`_WorstOfRecorder`, which buffers the sub-run outcomes and stamps the
row once with the WORST of them. A tenant with ``emit_egress = false`` has a single sub-run, and for
that case the wrapper replays exactly the call the connector made.

Multiple egress providers is the normal case
--------------------------------------------
``tenant_egress_config`` is keyed on ``(tenant_id, egress_provider)``, so a tenant can have AWS,
Cloudflare and Vercel egress at the same time. Every configured provider gets its own run with its
own provider-scoped recorder, because each provider produces a DISTINCT synthetic span id and the
three only sum correctly on ``/cost`` if all three ran.

Vercel double-count gate
------------------------
``tenant_vercel_config.emit_egress`` defaults false and exists so Vercel bandwidth reaches the
egress layer through exactly one path (CTO-163). ``VercelCostConnector`` reads the flag itself and
this job passes the loaded row through untouched: nothing here inspects, overrides or works around
it. ``enabled`` is honoured the same way, as the tenant's own pause switch.

Failure posture
---------------
A failure in one connector records ``failed`` on that connector's row, emits NO span (never a
guessed number), and does not stop the other connectors for the same tenant. Once every unit has had
its turn, the job raises if any of them failed, so the scheduler records ``failed`` for the tenant
and its backoff applies. A partially failed tenant is therefore ``failed`` at the schedule level and
the config rows say precisely which connector broke.

Re-running a day inserts nothing new. The base connector checks ``span_exists`` on the deterministic
synthetic span id before every insert, which is the backstop if the scheduler ever double-fires.
Nothing in this module weakens that: it picks the window and hands it to the connector. A widened
window is the same guarantee applied to more days, because a day that already has a span is skipped
whether it was reached by ``run()`` or by ``run_backfill()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone

from gateway.config import Settings
from gateway.connectors.base import BillingClient, RunRecorder, SpanSink, synthetic_span_id
from gateway.connectors.compute import ComputeCostConnector, build_billing_client
from gateway.connectors.config_store import (
    TenantComputeConfigStore,
    TenantEgressConfigStore,
    TenantVercelConfigStore,
)
from gateway.connectors.egress import EgressCostConnector, build_egress_client
from gateway.connectors.vercel import (
    VERCEL_PROVIDER,
    VercelCostConnector,
    VercelUsageClient,
    build_vercel_usage_client,
)
from gateway.scheduler import Job, JobRegistry, JobSkipped
from gateway.store import ClickHouseStore

logger = logging.getLogger("tally.gateway.cost_connector_job")

#: Persisted in ``scheduler_runs.job_name``, so it is a stable contract: renaming it orphans the
#: history and every tenant would look like it had never run, which would trigger an immediate run
#: for all of them.
JOB_NAME = "cost_connectors"

#: Daily. The providers themselves publish daily granularity, so a finer cadence would re-ask for a
#: number that cannot have changed and would spend a tenant's billing-API rate limit doing it.
DAILY_INTERVAL_S = 86400.0

#: The most billing days ONE invocation will cover (CTO-219). A connector that has been broken for
#: three months must not try to bill ninety days in a single run: that is a very long billing-API
#: call, and a job body that times out mid-window is worse than one that makes steady progress.
#: A week is comfortably more than any realistic scheduler outage while still being one cheap range
#: query at every provider. The remainder is NOT lost: the next run recomputes the last billed day,
#: which has moved forward by this much, and continues from there.
MAX_CATCHUP_DAYS = 7

#: How far back the search for "the last day actually billed" will probe before giving up
#: (CTO-219). Bounds the work of the search itself, which is one point lookup per day per
#: (provider, operation) series. A fortnight covers the outages a daily job realistically has (a
#: deploy freeze, a long weekend of a broken credential) and must stay LARGER than
#: :data:`MAX_CATCHUP_DAYS`, otherwise a gap could never be seen far enough back to be caught up in
#: instalments. Nothing billed inside the window means "start a fresh chain at yesterday": deeper
#: history is a job for ``scripts/backfill_*.py``, which the scheduler scope keeps for that purpose.
LOOKBACK_DAYS = 14


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def target_day(now: datetime) -> date:
    """The NEWEST day that can be billed: yesterday UTC. See the module docstring for why not today.

    Still the end of every window. What changed in CTO-219 is that it is no longer also the start:
    see :func:`billing_window`.
    """
    return (now.astimezone(timezone.utc) - timedelta(days=1)).date()


def last_billed_day(
    store: SpanSink, tenant_id: str, provider: str, operation: str, *, newest: date
) -> date | None:
    """The newest day at or before ``newest`` that already has a span for this series, if any.

    This is the "last day actually billed" the window is derived from (CTO-219). It asks the spans
    rather than a config column because the span IS the billing output for a day, and it asks with
    exactly the probe the emitter's idempotency guard uses, so the two can never disagree about what
    counts as billed.

    Returns ``None`` when nothing is billed within :data:`LOOKBACK_DAYS`, which is both a brand new
    connector and one that has been dark for longer than the job will catch up. The caller treats
    both as "start a fresh chain at yesterday".
    """
    day = newest
    oldest = newest - timedelta(days=LOOKBACK_DAYS - 1)
    while day >= oldest:
        _, span_id = synthetic_span_id(tenant_id, provider, operation, day)
        if store.span_exists(tenant_id, span_id):
            return day
        day -= timedelta(days=1)
    return None


def billing_window(
    store: SpanSink,
    tenant_id: str,
    series: Sequence[tuple[str, str]],
    *,
    newest: date,
) -> tuple[date, date]:
    """The ``(start_day, end_day)`` one connector should bill, inclusive (CTO-219).

    ``series`` is the ``(provider, operation)`` pairs this connector emits, which is one pair for
    compute and egress and two for Vercel with the egress gate on. The window starts the day after
    the OLDEST of their last-billed days, so a Vercel run whose egress half is behind does not leave
    that half behind for good, and re-covering a day the other half already has costs nothing
    because the emitter skips it.

    The window is capped at :data:`MAX_CATCHUP_DAYS` and never extends past ``newest``. A tenant
    that is already up to date gets ``(newest, newest)``, which is the pre-CTO-219 behaviour and the
    common case.
    """
    known = [
        billed
        for provider, operation in series
        if (billed := last_billed_day(store, tenant_id, provider, operation, newest=newest))
        is not None
    ]
    # Nothing billed inside the lookback window: bill yesterday alone rather than inventing a
    # backfill. See LOOKBACK_DAYS.
    start = newest if not known else min(known) + timedelta(days=1)
    if start > newest:
        start = newest
    end = min(start + timedelta(days=MAX_CATCHUP_DAYS - 1), newest)
    return start, end


class _WorstOfRecorder:
    """Buffers sub-run outcomes for ONE shared config row and stamps it with the worst (CTO-219).

    ``VercelCostConnector`` runs a compute sub-run and, when the CTO-163 gate is on, an egress
    sub-run, and both record onto the single ``tenant_vercel_config`` row. Recording as they went
    meant last-writer-wins, so a failed compute sub-run followed by a successful egress sub-run left
    a customer-facing row reading ``success`` while no compute span existed: a tenant looking at a
    healthy connector that was not producing data.

    The rule is worst-outcome, not last-outcome. ``success`` is only stamped when every sub-run that
    actually RAN succeeded. With the gate off there is exactly one sub-run and :meth:`flush` replays
    precisely the call the connector made, so that case is untouched.
    """

    def __init__(self, inner: RunRecorder) -> None:
        self._inner = inner
        self._calls: list[tuple[str, str, str, str | None]] = []

    def record_run(
        self, tenant_id: str, connector_id: str, status: str, *, error_message: str | None = None
    ) -> None:
        self._calls.append((tenant_id, connector_id, status, error_message))

    def flush(self) -> None:
        """Stamp the row once. A no-op if no sub-run recorded anything, so a row is never invented."""
        if not self._calls:
            return
        # The first failure, if there was one, so the message on the row names the sub-run that
        # actually broke rather than the one that happened to finish last. All-success falls back
        # to the FIRST call, which is the sub-run that always runs, so a gate-off tenant is stamped
        # with exactly the call its single sub-run made.
        worst = next((call for call in self._calls if call[2] != "success"), self._calls[0])
        self._calls.clear()
        tenant_id, connector_id, status, error_message = worst
        self._inner.record_run(tenant_id, connector_id, status, error_message=error_message)


class CostConnectorRunError(RuntimeError):
    """At least one of a tenant's connectors failed. Carries every failure, not just the first.

    Raised only AFTER every configured connector has had its turn, so one broken provider never
    costs a tenant the providers that were working.
    """


class CostConnectorJob:
    """Runs every cost connector a tenant has configured, for the days it owes. Callable as a job.

    Constructed once at startup and called with a tenant id per due tick. It holds no per-tenant
    state: config is re-read on every call, so connecting or disconnecting a cloud account takes
    effect on the next tick with no restart.

    Every collaborator is injectable so the tests never touch Postgres, ClickHouse or a cloud
    billing API. The defaults are the production wiring.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        compute_store: TenantComputeConfigStore | None = None,
        egress_store: TenantEgressConfigStore | None = None,
        vercel_store: TenantVercelConfigStore | None = None,
        store_factory: Callable[[], ClickHouseStore] | None = None,
        compute_client_factory: Callable[[str], BillingClient] = build_billing_client,
        egress_client_factory: Callable[[str], BillingClient] = build_egress_client,
        usage_client_factory: Callable[[], VercelUsageClient] = build_vercel_usage_client,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._compute_store = (
            compute_store if compute_store is not None else TenantComputeConfigStore(settings)
        )
        self._egress_store = (
            egress_store if egress_store is not None else TenantEgressConfigStore(settings)
        )
        self._vercel_store = (
            vercel_store if vercel_store is not None else TenantVercelConfigStore(settings)
        )
        # A span sink per invocation rather than one shared with the API. These are daily runs, so a
        # client per run costs nothing, and a job that owns its client cannot leave the request
        # path's client in a bad state or be left in one by it.
        self._store_factory = (
            store_factory if store_factory is not None else lambda: ClickHouseStore(settings)
        )
        self._compute_client_factory = compute_client_factory
        self._egress_client_factory = egress_client_factory
        self._usage_client_factory = usage_client_factory
        self._now = now

    def __call__(self, tenant_id: str) -> None:
        """Bill every configured connector for ``tenant_id``. Raises if any of them failed.

        Each connector gets its OWN window, because each has its own last-billed day: an egress
        provider added yesterday must not be dragged over a gap that belongs to compute.

        Blocking by design: the scheduler calls job bodies on a worker thread precisely so they can
        do blocking Postgres and HTTP work without touching the event loop.
        """
        compute_config = self._compute_store.load_config(tenant_id)
        egress_configs = self._egress_store.load_configs(tenant_id)
        vercel_config = self._vercel_store.load_config(tenant_id)
        if vercel_config is not None and not vercel_config.enabled:
            # The tenant's own pause switch. Paused is "nothing to do", not a failure, and if it is
            # the tenant's only connector then the whole tenant is a skip.
            vercel_config = None

        if compute_config is None and not egress_configs and vercel_config is None:
            # The normal state for most tenants. JobSkipped settles the cadence window without
            # claiming a run happened. See the module docstring.
            raise JobSkipped(f"tenant {tenant_id} has no cost connector configured")

        newest = target_day(self._now())
        failures: list[str] = []
        # Only for the error message: each connector bills its own window, so there is no single
        # day to name if they have drifted apart.
        days: list[date] = []
        # The connectors share ONE sink, so the span_exists idempotency guard sees every insert this
        # run makes, including the Vercel egress span whose id is identical to the CTO-144 one.
        store = self._store_factory()
        try:
            if compute_config is not None:
                provider = compute_config.cloud_provider
                start, end = billing_window(
                    store, tenant_id, [(provider, "compute")], newest=newest
                )
                days.append(end)
                self._attempt(
                    failures,
                    label=f"compute/{provider}",
                    tenant_id=tenant_id,
                    connector_id="compute",
                    recorder=self._compute_store,
                    # run_backfill rather than a loop over run(): the connectors already fetch a
                    # whole range in one billing-API call, and a single day is the same code path.
                    run=lambda cfg=compute_config, s=start, e=end: [
                        ComputeCostConnector(
                            store=store,
                            recorder=self._compute_store,
                            billing_client=self._compute_client_factory(cfg.cloud_provider),
                        )
                        .run_backfill(cfg, start_day=s, end_day=e)
                        .status
                    ],
                )

            for egress_config in egress_configs:
                start, end = billing_window(
                    store,
                    tenant_id,
                    [(egress_config.cloud_provider, "egress")],
                    newest=newest,
                )
                days.append(end)
                # `cfg` is bound per iteration on purpose: the lambda outlives the iteration that
                # built it, and a late-bound loop variable would run the last provider N times. The
                # window is bound the same way and for the same reason.
                self._attempt(
                    failures,
                    label=f"egress/{egress_config.cloud_provider}",
                    tenant_id=tenant_id,
                    connector_id="egress",
                    recorder=self._egress_store.recorder_for(egress_config.cloud_provider),
                    run=lambda cfg=egress_config, s=start, e=end: [
                        EgressCostConnector(
                            store=store,
                            recorder=self._egress_store.recorder_for(cfg.cloud_provider),
                            billing_client=self._egress_client_factory(cfg.cloud_provider),
                        )
                        .run_backfill(cfg, start_day=s, end_day=e)
                        .status
                    ],
                )

            if vercel_config is not None:
                # Both halves of a Vercel run share a window, and the gate decides whether the
                # egress series counts towards it. Reading the flag to pick the SERIES is not
                # interpreting the CTO-163 gate: the connector still decides whether to emit.
                series = [(VERCEL_PROVIDER, "compute")]
                if vercel_config.emit_egress:
                    series.append((VERCEL_PROVIDER, "egress"))
                start, end = billing_window(store, tenant_id, series, newest=newest)
                days.append(end)
                # One row, two sub-runs: the recorder is wrapped so the row ends up worst-of rather
                # than last-of (CTO-219, finding 2).
                vercel_recorder = _WorstOfRecorder(self._vercel_store)
                self._attempt(
                    failures,
                    label="vercel",
                    tenant_id=tenant_id,
                    connector_id="compute",
                    recorder=vercel_recorder,
                    # emit_egress rides on the config row and VercelCostConnector reads it itself:
                    # the CTO-163 double-count gate is not this job's to interpret.
                    run=lambda cfg=vercel_config, s=start, e=end: _vercel_statuses(
                        VercelCostConnector(
                            store=store,
                            usage_client=self._usage_client_factory(),
                            recorder=vercel_recorder,
                        ).run_backfill(cfg, start_day=s, end_day=e)
                    ),
                )
                self._flush(vercel_recorder, tenant_id)
        finally:
            # Closed on every path, including the raise below. A job that leaked a ClickHouse client
            # a day would take a long time to notice and would look like a ClickHouse problem.
            store.close()

        if failures:
            through = max(days).isoformat() if days else newest.isoformat()
            raise CostConnectorRunError(
                f"cost connectors failed for tenant {tenant_id} through {through}: "
                + "; ".join(failures)
            )

    def _attempt(
        self,
        failures: list[str],
        *,
        label: str,
        tenant_id: str,
        connector_id: str,
        recorder: RunRecorder,
        run: Callable[[], Sequence[str]],
    ) -> None:
        """Run one connector, collect its failure, and never let it end the tenant's other runs.

        The connector's own ``_run_range`` already records ``failed`` for a failed FETCH and returns
        a status rather than raising. This wrapper covers everything outside that: an unsupported
        provider, a client that cannot be constructed, a ClickHouse insert that fails. Those record
        ``failed`` here so the config row still tells the tenant their connector is broken, which is
        what that column is for.
        """
        try:
            statuses = run()
        except Exception as exc:  # noqa: BLE001 - one broken connector must not cost the others
            logger.warning(
                "cost connector job: tenant=%s connector=%s raised: %s: %s",
                tenant_id,
                label,
                type(exc).__name__,
                exc,
            )
            self._record_failed(recorder, tenant_id, connector_id, exc)
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            return
        for status in statuses:
            if status != "success":
                # Already recorded on the config row by the connector, with no span emitted.
                failures.append(f"{label}: {status}")

    @staticmethod
    def _flush(recorder: _WorstOfRecorder, tenant_id: str) -> None:
        """Stamp a buffered shared row. A recorder that cannot write must not fail the whole run."""
        try:
            recorder.flush()
        except Exception:  # noqa: BLE001 - the connector outcome is already in `failures`
            logger.warning(
                "cost connector job: tenant=%s could not record vercel status", tenant_id
            )

    @staticmethod
    def _record_failed(
        recorder: RunRecorder, tenant_id: str, connector_id: str, exc: Exception
    ) -> None:
        try:
            recorder.record_run(
                tenant_id, connector_id, "failed", error_message=f"{type(exc).__name__}: {exc}"
            )
        except Exception:  # noqa: BLE001 - the raise that brought us here is the real news
            logger.warning("cost connector job: tenant=%s could not record failed status", tenant_id)


def _vercel_statuses(result: object) -> list[str]:
    """Both halves of a Vercel run: compute always, egress only when the gate let it run."""
    compute = getattr(result, "compute", None)
    egress = getattr(result, "egress", None)
    statuses = [] if compute is None else [compute.status]
    if egress is not None:
        statuses.append(egress.status)
    return statuses


def register_cost_connector_job(
    registry: JobRegistry,
    settings: Settings,
    *,
    interval_s: float = DAILY_INTERVAL_S,
    job: CostConnectorJob | None = None,
) -> Job:
    """Register the daily cost-connector job on ``registry`` (called from the gateway lifespan).

    Kept here rather than inside ``build_scheduler`` so the scheduler core stays a job-free engine
    and each job's wiring lives next to the job.
    """
    return registry.register(
        JOB_NAME,
        interval_s,
        job if job is not None else CostConnectorJob(settings),
    )


__all__ = [
    "DAILY_INTERVAL_S",
    "JOB_NAME",
    "LOOKBACK_DAYS",
    "MAX_CATCHUP_DAYS",
    "CostConnectorJob",
    "CostConnectorRunError",
    "billing_window",
    "last_billed_day",
    "register_cost_connector_job",
    "target_day",
]
