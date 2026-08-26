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
CTO-176 writes, construct the connectors that already exist, run them for one day, and let them
record their own outcome.

Run recording has ONE owner per fact
------------------------------------
The connectors already stamp ``last_run_at`` / ``last_status`` on their own config row through the
``RunRecorder`` protocol, and this job passes those same recorders in rather than wrapping them.
There is no second "did the connector run" store here, because two sources of truth for one fact is
how a dashboard ends up disagreeing with itself. The division is:

* ``scheduler_runs`` answers "did the SCHEDULE fire for this tenant" (owned by the scheduler).
* ``tenant_*_config.last_status`` answers "did the CONNECTOR work" (owned by the connector).

An unconfigured tenant raises :class:`gateway.scheduler.JobSkipped` rather than returning success.
Most tenants have no cloud account connected, and a ``skipped`` row settles the cadence window (so
they are not re-asked every tick) while staying distinguishable from a real run, so a freshness
surface can never read "we ran, all good" off a tenant we never had anything to do for.

Which day
---------
Yesterday UTC, always (:func:`target_day`). Billing APIs only report a complete day once it has
closed, so asking for today would fetch a partial number. That partial number would then be FROZEN:
the emitter is keyed on ``(tenant, provider, operation, day)`` and skips a day that already has a
span, so nothing would ever correct it. One day of lag is the honest choice, and it is the same
window the backfill scripts default to.

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
Nothing in this module weakens that: it picks the day and calls ``run()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone

from gateway.config import Settings
from gateway.connectors.base import BillingClient, RunRecorder
from gateway.connectors.compute import ComputeCostConnector, build_billing_client
from gateway.connectors.config_store import (
    TenantComputeConfigStore,
    TenantEgressConfigStore,
    TenantVercelConfigStore,
)
from gateway.connectors.egress import EgressCostConnector, build_egress_client
from gateway.connectors.vercel import (
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def target_day(now: datetime) -> date:
    """The day to bill for: yesterday UTC. See the module docstring for why not today."""
    return (now.astimezone(timezone.utc) - timedelta(days=1)).date()


class CostConnectorRunError(RuntimeError):
    """At least one of a tenant's connectors failed. Carries every failure, not just the first.

    Raised only AFTER every configured connector has had its turn, so one broken provider never
    costs a tenant the providers that were working.
    """


class CostConnectorJob:
    """Runs every cost connector a tenant has configured, for one day. Callable as a job body.

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
        """Run every configured connector for ``tenant_id``. Raises if any of them failed.

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

        day = target_day(self._now())
        failures: list[str] = []
        # The connectors share ONE sink, so the span_exists idempotency guard sees every insert this
        # run makes, including the Vercel egress span whose id is identical to the CTO-144 one.
        store = self._store_factory()
        try:
            if compute_config is not None:
                provider = compute_config.cloud_provider
                self._attempt(
                    failures,
                    label=f"compute/{provider}",
                    tenant_id=tenant_id,
                    connector_id="compute",
                    recorder=self._compute_store,
                    run=lambda cfg=compute_config: [
                        ComputeCostConnector(
                            store=store,
                            recorder=self._compute_store,
                            billing_client=self._compute_client_factory(cfg.cloud_provider),
                        )
                        .run(cfg, day=day)
                        .status
                    ],
                )

            for egress_config in egress_configs:
                # `cfg` is bound per iteration on purpose: the lambda outlives the iteration that
                # built it, and a late-bound loop variable would run the last provider N times.
                self._attempt(
                    failures,
                    label=f"egress/{egress_config.cloud_provider}",
                    tenant_id=tenant_id,
                    connector_id="egress",
                    recorder=self._egress_store.recorder_for(egress_config.cloud_provider),
                    run=lambda cfg=egress_config: [
                        EgressCostConnector(
                            store=store,
                            recorder=self._egress_store.recorder_for(cfg.cloud_provider),
                            billing_client=self._egress_client_factory(cfg.cloud_provider),
                        )
                        .run(cfg, day=day)
                        .status
                    ],
                )

            if vercel_config is not None:
                self._attempt(
                    failures,
                    label="vercel",
                    tenant_id=tenant_id,
                    connector_id="compute",
                    recorder=self._vercel_store,
                    # emit_egress rides on the config row and VercelCostConnector reads it itself:
                    # the CTO-163 double-count gate is not this job's to interpret.
                    run=lambda cfg=vercel_config: _vercel_statuses(
                        VercelCostConnector(
                            store=store,
                            usage_client=self._usage_client_factory(),
                            recorder=self._vercel_store,
                        ).run(cfg, day=day)
                    ),
                )
        finally:
            # Closed on every path, including the raise below. A job that leaked a ClickHouse client
            # a day would take a long time to notice and would look like a ClickHouse problem.
            store.close()

        if failures:
            raise CostConnectorRunError(
                f"cost connectors failed for tenant {tenant_id} on {day.isoformat()}: "
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
    "CostConnectorJob",
    "CostConnectorRunError",
    "register_cost_connector_job",
    "target_day",
]
