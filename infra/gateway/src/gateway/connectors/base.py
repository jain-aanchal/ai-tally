"""Reusable base for daily cloud-billing connectors (CTO-143).

The pattern every cloud-billing connector shares:

  1. Load a per-tenant control-plane config row (which provider, which credential reference, which
     cost-allocation tag filter).
  2. Fetch the day's spend from the provider's billing API — behind an INJECTABLE client so tests
     never touch the network and so egress (CTO-144) can plug in its own fetchers.
  3. Aggregate to one total per day and emit ONE synthetic span carrying that total as the cost.
  4. Record the run outcome (success / failed) on the control-plane row.

Only step 2 differs by connector, so it is the single abstract method; everything else lives here.
The class is operation-agnostic: a subclass sets ``operation`` to ``'compute'`` or ``'egress'`` and
the same emitter / run contract / recorder are reused verbatim.

Synthetic-span approach (matches the gateway ingest path in :mod:`gateway.mapping`): the cost is
already authoritative from the billing API, so we set ``gen_ai.cost.estimated_micro_usd`` directly
and reuse :func:`gateway.mapping.span_to_row` — we do NOT route through catalog cost enrichment
(there is no model to price). ``CostSource`` lands as ``'estimated'`` (mapping's default), which is
correct: it's an estimate from the provider's *un-invoiced* Cost Explorer / Billing numbers, not a
reconciled invoice.

Idempotency: ``otel_spans`` is a plain ``MergeTree`` (no dedup), so re-running a day would
double-count. Each synthetic span gets a DETERMINISTIC id derived from
``(tenant_id, provider, operation, day)`` (:func:`synthetic_span_id`); the emitter checks
``store.span_exists`` before inserting and skips a day that is already present. Backfill re-runs are
therefore safe.

Honest-under-uncertainty: a failed fetch records a ``failed`` run and emits NO span — never a
guessed number.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

from tally.schema import GenAI

from gateway.mapping import span_to_row

logger = logging.getLogger(__name__)

# Fixed identity for synthetic billing spans. Not a real service/span — labelled so they're
# obviously connector-emitted when someone eyeballs otel_spans.
_SERVICE_NAME = "cloud-billing"


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    """One tenant's cloud-billing connector config — the loaded control-plane row.

    ``credentials_ref`` is a Secret Manager / KMS / ARN reference (or ``'aws-default-chain'``),
    NEVER raw credentials. ``tag_filter`` is the cost-allocation tag set the billing query is scoped
    to (default ``{'tally:workload': 'ai'}``).
    """

    tenant_id: str
    cloud_provider: str  # 'aws' | 'gcp'
    credentials_ref: str
    tag_filter: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DailyCost:
    """A provider's spend for a single UTC day, in integer micro-USD."""

    day: date
    cost_micro_usd: int


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of one connector run for one tenant.

    ``status`` is one of ``'success' | 'failed'``. On failure ``spans_emitted == 0`` and
    ``error_message`` is set; on success ``spans_emitted`` counts newly-inserted days (days already
    present are skipped by the idempotency guard and don't inflate the count).
    """

    tenant_id: str
    operation: str
    provider: str
    status: str
    spans_emitted: int
    cost_micro_usd: int
    error_message: str | None = None


@runtime_checkable
class BillingClient(Protocol):
    """Provider-specific fetch, injected so tests never hit a live cloud API.

    Implementations (AWS Cost Explorer / GCP Cloud Billing in :mod:`gateway.connectors.compute`)
    translate a recorded/paginated billing response into per-day totals scoped to ``config``.
    """

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        """Return one :class:`DailyCost` per day in ``[start_day, end_day]`` that has spend."""
        ...


@runtime_checkable
class SpanSink(Protocol):
    """The slice of :class:`gateway.store.ClickHouseStore` the emitter needs (fake-able in tests)."""

    def span_exists(self, tenant_id: str, span_id: str) -> bool: ...

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int: ...


@runtime_checkable
class RunRecorder(Protocol):
    """Records a connector run outcome on the tenant's control-plane row.

    Mirrors :meth:`gateway.tenant_integrations.TenantIntegrationStore.record_run`. Egress reuses the
    same shape — ``connector_id`` distinguishes ``'compute'`` from ``'egress'``.
    """

    def record_run(
        self, tenant_id: str, connector_id: str, status: str, *, error_message: str | None = None
    ) -> None: ...


def synthetic_span_id(tenant_id: str, provider: str, operation: str, day: date) -> tuple[str, str]:
    """Deterministic ``(trace_id, span_id)`` for a connector's synthetic span.

    Stable across runs so re-emitting the same ``(tenant, provider, operation, day)`` produces the
    same ids — the idempotency guard keys off ``span_id``. TraceId is 32 hex chars, SpanId 16, to
    match the otel id widths the rest of the pipeline assumes.
    """
    digest = hashlib.sha256(
        f"{tenant_id}|{provider}|{operation}|{day.isoformat()}".encode()
    ).hexdigest()
    return digest[:32], digest[32:48]


class CloudBillingConnector(ABC):
    """Base daily connector. Subclass, set :attr:`operation`, implement :meth:`fetch_daily_costs`.

    Parameters
    ----------
    store:
        Span sink (``ClickHouseStore`` in prod; a fake in tests). Used to check existence and insert.
    recorder:
        Run-status recorder (the compute config store in prod). May be ``None`` in unit tests that
        only exercise emission.
    billing_client:
        The injected provider fetcher. A subclass may instead override :meth:`fetch_daily_costs` to
        dispatch across several clients (compute does this for aws vs. gcp).
    """

    #: Set by subclass — the ``gen_ai.operation.name`` written on synthetic spans and the
    #: connector_id used for run recording. ``'compute'`` here; ``'egress'`` in CTO-144.
    operation: str = ""

    def __init__(
        self,
        *,
        store: SpanSink,
        recorder: RunRecorder | None = None,
        billing_client: BillingClient | None = None,
    ) -> None:
        if not self.operation:
            raise ValueError("subclass must set a non-empty `operation`")
        self._store = store
        self._recorder = recorder
        self._client = billing_client

    # --- the one thing subclasses customize -----------------------------------------------------

    @abstractmethod
    def fetch_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        """Fetch per-day spend for ``config`` over ``[start_day, end_day]`` (inclusive).

        Must raise on a failed fetch — :meth:`run` turns that into a ``failed`` run with no span.
        """
        raise NotImplementedError

    # --- shared daily-run contract --------------------------------------------------------------

    def run(self, config: ConnectorConfig, *, day: date) -> RunResult:
        """Fetch, emit, and record for a single ``day``.

        The daily-cron entry point.
        """
        return self._run_range(config, start_day=day, end_day=day)

    def run_backfill(self, config: ConnectorConfig, *, start_day: date, end_day: date) -> RunResult:
        """Backfill ``[start_day, end_day]`` in one fetch. Idempotent — re-running skips days that
        already have a synthetic span (see :meth:`emit_cost_span`), so no double-counting.
        """
        return self._run_range(config, start_day=start_day, end_day=end_day)

    def _run_range(self, config: ConnectorConfig, *, start_day: date, end_day: date) -> RunResult:
        provider = config.cloud_provider
        try:
            costs = self.fetch_daily_costs(config, start_day=start_day, end_day=end_day)
        except Exception as exc:  # noqa: BLE001 — a failed fetch must NOT emit a guessed span.
            logger.warning(
                "compute connector fetch failed tenant=%s provider=%s: %s",
                config.tenant_id,
                provider,
                exc,
            )
            self._record(config.tenant_id, "failed", error_message=str(exc))
            return RunResult(
                tenant_id=config.tenant_id,
                operation=self.operation,
                provider=provider,
                status="failed",
                spans_emitted=0,
                cost_micro_usd=0,
                error_message=str(exc),
            )

        emitted = 0
        total_micro = 0
        for dc in costs:
            total_micro += dc.cost_micro_usd
            if self.emit_cost_span(
                config.tenant_id,
                cost_micro_usd=dc.cost_micro_usd,
                day=dc.day,
                provider=provider,
            ):
                emitted += 1

        self._record(config.tenant_id, "success")
        return RunResult(
            tenant_id=config.tenant_id,
            operation=self.operation,
            provider=provider,
            status="success",
            spans_emitted=emitted,
            cost_micro_usd=total_micro,
        )

    # --- synthetic-span emitter -----------------------------------------------------------------

    def emit_cost_span(
        self,
        tenant_id: str,
        *,
        cost_micro_usd: int,
        day: date,
        provider: str,
        feature_tag: str | None = None,
    ) -> bool:
        """Emit one synthetic cost span for ``day`` carrying ``cost_micro_usd`` as EstimatedCost.

        Returns ``True`` if a row was inserted, ``False`` if it was skipped because a span for this
        ``(tenant, provider, operation, day)`` already exists (idempotency guard) or the cost is
        non-positive (nothing to record — a $0 day is not worth a synthetic span).

        The cost is set DIRECTLY (no catalog enrichment) and the row is built with the same
        :func:`gateway.mapping.span_to_row` the live ingest path uses, so the synthetic row is
        column-for-column consistent with real spans.
        """
        if cost_micro_usd <= 0:
            return False

        trace_id, span_id = synthetic_span_id(tenant_id, provider, self.operation, day)
        if self._store.span_exists(tenant_id, span_id):
            return False

        # Timestamp the span at noon UTC on its day so toDate(Timestamp) partitions it onto the
        # correct calendar day regardless of the reader's timezone rounding.
        ts = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)
        effective_ts_ns = int(ts.timestamp() * 1_000_000_000)

        attrs: dict[str, object] = {
            "TraceId": trace_id,
            "SpanId": span_id,
            "ServiceName": _SERVICE_NAME,
            "SpanName": f"{self.operation}.daily",
            GenAI.OPERATION_NAME: self.operation,
            GenAI.SYSTEM: provider,
            GenAI.COST_ESTIMATED_MICRO_USD: int(cost_micro_usd),
            GenAI.COST_CURRENCY: "USD",
            GenAI.FEATURE_TAG: feature_tag or "untagged",
        }
        row = span_to_row(attrs, tenant_id=tenant_id, effective_ts_ns=effective_ts_ns)
        self._store.insert_spans([row])
        return True

    # --- run recording --------------------------------------------------------------------------

    def _record(self, tenant_id: str, status: str, *, error_message: str | None = None) -> None:
        if self._recorder is None:
            return
        self._recorder.record_run(
            tenant_id, self.operation, status, error_message=error_message
        )
