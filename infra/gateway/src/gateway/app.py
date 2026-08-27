"""FastAPI ingest gateway.

POST /v1/batches — accept a :class:`tally.wire.BatchRequest` (JSON), authenticate, dedupe on
(tenant_id, batch_id), enrich each span's cost authoritatively, clamp clock skew, and write spans +
business events + identity links into ClickHouse.

The heavy lifting (envelope, idempotency, cost recompute, skew clamp) is the SDK's already-tested
pure logic — this module is just the HTTP + storage shell.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from tally.account_identity import AccountLinker
from tally.enrichment import enrich_cost
from tally.models import discover_models
from tally.pricing import seed_catalog
from tally.schema import GenAI
from tally.timekeeping import assess
from tally.wire import (
    BatchRequest,
    BatchResponse,
    BusinessEvent,
    IdempotencyCache,
    IdentityLink,
    PartialError,
    Sampling,
    ServerHints,
    Status,
    uuid7,
)

from gateway.account_lookup import (
    AccountLookupError,
    hash_account_id,
    normalize_account_id,
)
from gateway.auth import ApiKeyAuth
from gateway.backpressure import Backpressure
from gateway.config import get_settings
from gateway.cost_connector_job import register_cost_connector_job
from gateway.errors import ErrorCode
from gateway.ingest_buffer import AsyncIngestBuffer
from gateway.mapping import span_to_row
from gateway.metering import UsageRollup
from gateway.protocol import (
    SUPPORTED_PROTOCOLS,
    capabilities,
    negotiate,
    otlp_traces_to_spans,
)
from gateway.ratelimit import RateLimiter
from gateway.reconciliation import ReconciliationStore
from gateway.scheduler import JobRegistry, build_scheduler
from gateway.stitcher_job import register_stitcher_job
from gateway.store import ClickHouseStore
from gateway.stripe_ingest import (
    StripeSignatureError,
    hash_customer_email,
    hash_stripe_customer,
    map_stripe_event,
    verify_stripe_signature,
)
from gateway.replay_estimate import apply_system_prompt_override
from gateway.replay_executor import (
    CandidateCall,
    CandidateResponse,
    ReplayExecutor,
)
from gateway.replay_sampler import (
    SampleCandidate,
    build_payloads,
    stratified_sample,
)
from gateway.replay_store import (
    GCSReplayBlobStore,
    InMemoryReplayBlobStore,
    ReplayBlobStore,
    S3ReplayBlobStore,
    persist_sample,
)
from gateway.eval_executor import (
    EvalExecutor,
    JudgeCall,
    JudgeResponse,
)
from gateway.tenant_cac import (
    CacFormInput,
    CacPeriodError,
    TenantCacStore,
    csv_template,
    parse_csv,
)
from gateway.connectors.config_admin import (
    ALL_CONNECTORS,
    ConfigError,
    CostConnectorAdmin,
)
from gateway.tenant_account_labels import (
    AccountLabelError,
    TenantAccountLabelStore,
    TenantNotFound as AccountLabelTenantNotFound,
    normalize_account_id_hash,
    normalize_label,
)
from gateway.tenant_allocation import (
    ALLOCATION_RULES,
    DEFAULT_ALLOCATION_RULE,
    AllocationConfigError,
    TenantAllocationStore,
    TenantNotFound as AllocationTenantNotFound,
    normalize_rule,
    normalize_updated_by,
)
from gateway.tenant_budgets import (
    BUDGET_PERIODS,
    BUDGET_SCOPE_KINDS,
    BudgetError,
    BudgetOverlapError,
    TenantBudgetStore,
    TenantNotFound as BudgetTenantNotFound,
    normalize_budget_id,
)
from gateway.tenant_connectors import ALLOWED_LAYERS, TenantConnectorStore
from gateway.tenant_eval import TenantEvalStore
from gateway.tenant_feature_value_events import TenantFeatureValueEventStore
from gateway.tenant_identity import TenantIdentityResolver
from gateway.tenant_guardrails import (
    ALLOWED_KINDS as GUARDRAIL_KINDS,
    ALLOWED_STATES as GUARDRAIL_STATES,
    TenantGuardrailStore,
)
from gateway.tenant_integrations import TenantIntegrationStore
from gateway.tenant_replay import TenantReplayStore
from gateway.revenue_upload import (
    UPLOAD_SOURCE,
    RevenueUploadError,
    RevenueUploadStore,
    build_period_snapshots,
    normalize_period,
    parse_revenue_csv,
    period_id_prefix,
)
from gateway.tenant_lookup import TenantNotFoundError
from gateway.tenant_revenue_sources import (
    RevenueSourceConfigError,
    RevenueSourceConfigInput,
    TenantRevenueSourceStore,
)
from gateway.revenue_api import revenue_policy_note, to_wire_event
from gateway.tenant_stripe import TenantStripeStore
from gateway.tenant_unit_economics import (
    TenantUnitEconomicsStore,
    UnitEconomicsConfigError,
    UnitEconomicsConfigInput,
)
from gateway.validation import SpanValidator, span_item_id
from gateway.worker_jobs import register_worker_jobs

from tally.cdp_connectors import RevenuePayloadError, WebhookIngestor
from tally.hmac_keys import HmacKeyRegistry

logger = logging.getLogger("tally.gateway")

# CTO-237: bound on the in-memory replay sample index, per tenant, newest-wins. The index is read
# on the hot /v1/replay + /v1/eval paths and persists for the process lifetime; capping it keeps a
# high-volume tenant from growing it without limit while leaving a healthy corpus to project from.
REPLAY_INDEX_PER_TENANT_CAP = 500
# How many recent replay_samples rows to pull back from ClickHouse on boot to re-hydrate the
# in-memory index (CTO-237). Bounded so a large corpus does not blow up startup; the per-tenant cap
# above then trims each tenant's slice to REPLAY_INDEX_PER_TENANT_CAP.
REPLAY_HYDRATE_LIMIT = 5000

# Guards _configure_logging so a re-created app (e.g. the test suite spinning up many TestClients in
# one process) attaches the root handler exactly once and never stacks duplicates. See CTO-218.
_logging_configured = False


def _configure_logging(level_name: str) -> None:
    """Install one root stderr handler so gateway INFO lines are actually visible (CTO-218).

    The gateway is started with ``uvicorn gateway.app:app`` (see the Dockerfile CMD). uvicorn
    configures its own ``uvicorn*`` loggers but leaves the ROOT logger without a handler, so Python's
    lastResort handler applied and only WARNING and above reached stderr. Every gateway ``logger.info``
    (the ``tally.gateway*`` namespace: startup line, ingest buffer enablement, scheduler per-tick
    summary) was therefore silently discarded in a compose deployment.

    We attach a single handler to the root logger at the configured level. uvicorn's own loggers set
    ``propagate=False``, so their records are handled by uvicorn's handlers and never reach this one,
    which is why gateway lines appear exactly once and uvicorn's own lines are not doubled. Idempotent
    so it is safe to call at the very start of every ``lifespan``.
    """
    global _logging_configured
    if _logging_configured:
        return
    level = logging.getLevelName((level_name or "INFO").upper())
    # getLevelName returns the "Level <n>" string for an unknown name; fall back to INFO then.
    if not isinstance(level, int):
        level = logging.INFO
    handler = logging.StreamHandler()  # defaults to stderr, matching uvicorn's own default handler
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    _logging_configured = True


def _build_replay_blob_store(settings) -> ReplayBlobStore:
    """Select the replay blob-store backend from settings (CTO-152).

    ``memory`` (default) -> in-process dict store; ``gcs`` -> Google Cloud Storage via ADC /
    Workload Identity; ``s3`` -> AWS S3 (or S3-compatible, e.g. MinIO) via the AWS default
    credential chain. Each cloud client + its optional dependency (``google-cloud-storage`` /
    ``boto3``) is only touched when that backend is actually selected (lazy import lives inside the
    store), so the default install/boot never needs either package.
    """
    backend = (settings.replay_blob_backend or "memory").lower()
    if backend == "memory":
        return InMemoryReplayBlobStore()
    if backend == "gcs":
        if not settings.replay_gcs_bucket:
            raise ValueError(
                "TALLY_REPLAY_GCS_BUCKET must be set when TALLY_REPLAY_BLOB_BACKEND=gcs"
            )
        return GCSReplayBlobStore(bucket=settings.replay_gcs_bucket)
    if backend == "s3":
        if not settings.replay_s3_bucket:
            raise ValueError(
                "TALLY_REPLAY_S3_BUCKET must be set when TALLY_REPLAY_BLOB_BACKEND=s3"
            )
        return S3ReplayBlobStore(
            bucket=settings.replay_s3_bucket,
            prefix=settings.replay_s3_prefix,
            region=settings.replay_s3_region or None,
        )
    raise ValueError(
        f"unknown TALLY_REPLAY_BLOB_BACKEND: {backend!r} (expected 'memory', 'gcs', or 's3')"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Configure logging first so the startup INFO line below (and every other gateway logger.info)
    # is captured rather than dropped by uvicorn's WARNING-only root (CTO-218).
    _configure_logging(settings.log_level)
    app.state.settings = settings
    app.state.store = ClickHouseStore(settings)
    app.state.auth = ApiKeyAuth(settings)
    app.state.tenant_connectors = TenantConnectorStore(settings)
    # CTO-176: write path for the cloud cost-connector config rows (compute / egress /
    # vercel). Until now those rows could only be inserted straight into Postgres.
    app.state.cost_connector_admin = CostConnectorAdmin(settings)
    # Per-tenant guardrail registry (CTO-116) — the SDK polls /v1/tenant/guardrails on its
    # config-refresh window and enforces matching rules in-process. Shadow rules emit span
    # attrs but never alter the call; enabled rules do.
    app.state.tenant_guardrails = TenantGuardrailStore(settings)
    # Per-tenant feature value-event config (CTO-140): onboarding pins each feature's ROI to a
    # business value event. The /features page reads/writes via /v1/tenant/feature-value-events.
    app.state.tenant_feature_value_events = TenantFeatureValueEventStore(settings)
    app.state.tenant_stripe = TenantStripeStore(settings)
    # Per-tenant third-party integration run status (CTO-117): Stripe / Segment / HubSpot / Pendo.
    # Workers call .record_run after each cycle; the dashboard reads via /v1/tenant/integrations/status.
    app.state.tenant_integrations = TenantIntegrationStore(settings)
    # Reconciler run log (CTO-139): late-arrival tracking for the /features attribution diagnostics
    # card. run_reconciliation() scans recent events vs. matched spans and calls .record_run; the
    # dashboard reads the latest via GET /v1/tenant/reconciliation/status.
    app.state.reconciliation = ReconciliationStore(settings)
    # Per-tenant monthly CAC inputs (CTO-111): finance fills serially, locked when next month opens.
    app.state.tenant_cac = TenantCacStore(settings)
    # Per-tenant LTV/CAC band thresholds (CTO-126): overrides the hardcoded B2B-SaaS defaults the
    # dashboard's ltvCacBand/paybackBand classifiers use. A tenant with no row keeps the defaults.
    app.state.tenant_unit_economics = TenantUnitEconomicsStore(settings)
    # Per-tenant revenue source config (CTO-194): which business_events.Source values count as
    # revenue on /attribution. A tenant with no row counts every source and discriminates on
    # ValueType, which is what replaced the old hardcoded Source='stripe' filter.
    app.state.tenant_revenue_sources = TenantRevenueSourceStore(settings)
    # Uploaded-revenue manifest (CTO-198): one row per (tenant, period) recording WHEN that
    # period's snapshot was taken. The money itself lives in business_events like every other
    # revenue source; this is only what lets the dashboard say "as of" and go stale honestly.
    app.state.revenue_uploads = RevenueUploadStore(settings)
    # Generic revenue API (CTO-199): the SDK's WebhookIngestor, shared between the connector
    # webhooks and POST /v1/revenue/events so one deduplicator covers both. This is only the fast
    # in-process guard; the durable idempotency is the ClickHouse probe in the endpoint, which is
    # what still holds after a restart or across replicas.
    app.state.revenue_ingestor = WebhookIngestor()
    # Replay infra (CTO-113): per-tenant opt-in sampling + cross-provider projection.
    # The blob store is in-memory by default — swappable for MinIO/S3 via app.state override in
    # a deployment shim. Replay runs accumulate in-memory until ClickHouse writeback lands
    # (sink wired to a list for v1 — the projection API reads from it directly).
    app.state.tenant_replay = TenantReplayStore(settings)
    app.state.replay_blob_store = _build_replay_blob_store(settings)
    app.state.replay_sample_index = []  # list[ReplaySampleRow]
    # Hydrate the in-memory replay index from ClickHouse so /v1/replay + /v1/eval serve a captured
    # corpus after a restart, not just what this process has ingested since boot (CTO-237). The
    # index resets to [] every boot, so without this a fresh gateway would show an empty Compare /
    # eval even though the samples are durably persisted. Fail-soft: a fresh or unreachable
    # ClickHouse (or a not-yet-migrated replay_samples table) just leaves the index empty and boots.
    try:
        hydrated = app.state.store.recent_replay_samples(REPLAY_HYDRATE_LIMIT)
        # recent_replay_samples returns newest-first; reverse to chronological so the bounded append
        # keeps the newest per tenant when trimming.
        _extend_replay_index_bounded(app.state.replay_sample_index, list(reversed(hydrated)))
        if hydrated:
            logger.info("replay: hydrated %d sample(s) from ClickHouse", len(hydrated))
    except Exception as exc:  # noqa: BLE001 - hydrate must never crash boot
        logger.warning("replay: hydrate skipped (%s)", exc)
    app.state.replay_runs = []  # list[ReplayRunRow]
    # Eval harness (CTO-114): pairwise-LLM-judge over the replay outputs. Opt-in like replay;
    # judge calls accumulate in-memory until the ClickHouse writeback path lands.
    app.state.tenant_eval = TenantEvalStore(settings)
    app.state.eval_runs = []  # list[EvalRunRow]
    # Per-tenant HMAC key registry — used to hash Stripe customer emails into the same
    # UserIdHash space the SDK uses, so the attribution join lights up (CTO-110).
    app.state.hmac_registry = HmacKeyRegistry()
    # Tenant name <-> tenants.id UUID (CTO-185). Both spellings reach the gateway and each derives
    # a different HMAC key, so /v1/tenant/account-lookup hashes under every spelling rather than
    # guessing one and returning a hash that silently matches nothing.
    app.state.tenant_identity = TenantIdentityResolver(settings)
    # Optional human-readable account names (CTO-186). Kept in Postgres and joined at render time
    # so ClickHouse never holds a customer name: the label is mutable metadata and stamping it on
    # every span would both waste storage and defeat the point of AccountIdHash.
    app.state.tenant_account_labels = TenantAccountLabelStore(settings)
    # Per-tenant shared-cost allocation rule (CTO-193). Decides how compute and egress are split
    # across accounts on /cost-per-customer, which is roughly half of every figure on that page.
    # A tenant with no row gets pro_rata_direct and the page says the rule is the default.
    app.state.tenant_allocation = TenantAllocationStore(settings)
    # What the tenant intends to SPEND on AI (CTO-205). The first thing in this system to record a
    # customer's intent rather than our own metering, and every "versus budget" number in the
    # forecasting epic reads from it. No row is the normal state and means "no budget set".
    app.state.tenant_budgets = TenantBudgetStore(settings)
    # In-process dedup set for Stripe webhook redeliveries. ClickHouse's ReplacingMergeTree
    # will collapse late duplicates at merge time, but this short-circuits the second insert
    # so the 200 stays well under Stripe's 30s timeout window.
    app.state.stripe_event_seen = set()
    # CTO-195: per-tenant user→account map shared by every revenue connector in this process.
    # It learns from events that state both, and refuses to answer for a user seen against two
    # accounts (see tally.account_identity). In-process for the same reason the dedup set above
    # is: losing it on restart costs an honest blank, never a wrong account.
    app.state.account_linker = AccountLinker()
    app.state.catalog = seed_catalog()
    app.state.idempotency = IdempotencyCache(ttl_seconds=settings.idempotency_ttl_s)
    app.state.limiter = RateLimiter(
        rps=settings.rate_limit_rps,
        burst=settings.rate_limit_burst,
        monthly_quota=settings.monthly_quota_spans,
    )
    # Known feature tags aren't loaded yet (per-tenant Postgres lookup is a follow-up), so the
    # unknown-tag flag is disabled for now — schema + PII checks are always on.
    app.state.validator = SpanValidator(max_span_bytes=settings.max_span_bytes)
    app.state.backpressure = Backpressure(soft_limit=settings.backpressure_soft_limit)
    # HEAD-path billing meter (CTO-84/85/86): counts distinct traces + feature tags before any
    # sampling/shed so the bill is exact regardless of analytics sample rate.
    app.state.metering = UsageRollup()
    app.state.in_flight = 0
    # Ingest burst buffer (CTO-37): when enabled, spans are written to ClickHouse off the hot path by
    # a background drain loop, so a burst can't produce 5xx. Disabled → synchronous write (None).
    app.state.ingest_buffer = None
    if settings.ingest_buffered:
        buffer = AsyncIngestBuffer(
            app.state.store,
            capacity=settings.ingest_buffer_capacity,
            drain_batch=settings.ingest_buffer_drain_batch,
            poll_interval_s=settings.ingest_buffer_poll_interval_s,
        )
        await buffer.start()
        app.state.ingest_buffer = buffer
        logger.info("ingest buffer enabled (capacity=%d)", settings.ingest_buffer_capacity)
    # Scheduler (CTO-213): periodic per-tenant job execution. A tick loop that asks each registered
    # job "are you due for this tenant" and answers from run history in Postgres, rather than
    # sleeping for a day and losing its place on the next redeploy. Disabled → no task at all
    # (None), which is byte-identical to the behaviour before this landed.
    # Safe on multiple replicas as of CTO-214: per (job, tenant) Postgres advisory locks, so two
    # gateways cannot run the same job at once.
    #
    # Two ticket bodies register jobs here, and both switch on code that was written, tested and
    # called by nobody:
    #   * CTO-215, the daily cloud cost connectors. A tenant could connect AWS / GCP / Vercel /
    #     Cloudflare on /connectors and nothing ever acted on that config. Enabling the scheduler
    #     makes the Compute and Egress columns populate on their own, which CHANGES tenants'
    #     numbers (see docs/scheduler-scope.md).
    #   * CTO-216, the third-party ingest workers and the reconciler.
    #   * CTO-200, the attribution stitcher runner. This is what finally populates
    #     attribution_records, so /features stops showing honest nulls for value, payback and
    #     attribution rate for a tenant whose touches and value events actually overlap.
    app.state.scheduler = None
    if settings.scheduler_enabled:
        job_registry = JobRegistry()
        register_cost_connector_job(job_registry, settings)  # CTO-215
        register_stitcher_job(job_registry, settings)  # CTO-200
        register_worker_jobs(
            job_registry,
            settings,
            store=app.state.store,
            # Shared with the request path on purpose: a second HMAC registry would write into a
            # different UserIdHash space, and a second linker would learn nothing. See worker_jobs.
            hmac_registry=app.state.hmac_registry,
            account_linker=app.state.account_linker,
            integrations=app.state.tenant_integrations,
            reconciliation_store=app.state.reconciliation,
        )
        scheduler = build_scheduler(settings, job_registry)
        await scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "scheduler enabled (tick=%.0fs, jobs=%d)",
            settings.scheduler_tick_interval_s,
            scheduler.job_count,
        )
    # Auto-discover provider model lineups (CTO-109). Fail-soft: if both providers
    # are unreachable AND there's no cached file, we still boot — just with an empty
    # list and a WARNING. Demos read app.state.models so they don't hardcode SKUs
    # like claude-3-5-haiku-latest that the provider may retire out from under them.
    try:
        app.state.models = discover_models()
        if app.state.models:
            openai_ids = sorted(m.id for m in app.state.models if m.provider == "openai")
            anth_ids = sorted(m.id for m in app.state.models if m.provider == "anthropic")
            logger.info("models: openai=%s anthropic=%s", openai_ids, anth_ids)
        else:
            logger.warning("models: discovery returned no entries — booting without a lineup")
    except Exception as exc:  # noqa: BLE001 — discovery must never crash boot
        logger.warning("models: discovery raised, defaulting to empty list: %s", exc)
        app.state.models = []
    logger.info("gateway up (require_api_key=%s)", settings.require_api_key)
    yield
    # Shutdown order matters (CTO-219). The buffer is flushed FIRST, before anything is allowed to
    # wait on the scheduler: it holds accepted customer telemetry that is not durable anywhere yet,
    # while the scheduler holds jobs that are re-run from recorded history on the next tick. Stopping
    # the scheduler first meant a SIGTERM arriving mid-job blocked the flush behind that job, and the
    # buffered spans died with the process. Nothing about the flush depends on the scheduler's state.
    if app.state.ingest_buffer is not None:
        await app.state.ingest_buffer.stop()  # flush buffered rows before closing the store
    if app.state.scheduler is not None:
        # Still before the stores close, because a job in flight is holding one of them. The wait is
        # bounded (settings.scheduler_shutdown_timeout_s): a job runs on a thread that cannot be
        # cancelled, so past the bound it is left to die with the process. See Scheduler.stop.
        await app.state.scheduler.stop()
    app.state.store.close()


app = FastAPI(title="ai-tally ingest gateway", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _in_flight_gauge(request: Request, call_next: Any) -> Any:
    """Track concurrent ingest requests so backpressure can read live load (CTO-36)."""
    is_ingest = request.url.path == "/v1/batches"
    if is_ingest:
        app.state.in_flight = getattr(app.state, "in_flight", 0) + 1
    try:
        return await call_next(request)
    finally:
        if is_ingest:
            app.state.in_flight = max(0, getattr(app.state, "in_flight", 1) - 1)


def _parse_batch(payload: dict[str, Any]) -> BatchRequest:
    try:
        return BatchRequest(
            tenant_id=payload["tenant_id"],
            sdk_version=payload.get("sdk_version", "unknown"),
            resource_spans=payload.get("resource_spans", []),
            business_events=[BusinessEvent(**e) for e in payload.get("business_events", [])],
            identity_links=[IdentityLink(**x) for x in payload.get("identity_links", [])],
            sampling=Sampling(**payload.get("sampling", {})),
            batch_id=payload.get("batch_id") or uuid7(),
            client_send_ts_ns=payload.get("client_send_ts_ns", time.time_ns()),
        )
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed batch: {exc}") from exc


def _error(status_code: int, code: ErrorCode, message: str) -> HTTPException:
    """An HTTPException whose detail carries a stable wire error code clients can branch on."""
    return HTTPException(status_code=status_code, detail={"code": code.value, "message": message})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    store: ClickHouseStore = app.state.store
    auth: ApiKeyAuth = app.state.auth
    checks: dict[str, bool] = {}
    try:
        checks["clickhouse"] = store.ping()
    except Exception as exc:  # noqa: BLE001 - report, don't crash readiness
        logger.warning("clickhouse not ready: %s", exc)
        checks["clickhouse"] = False
    try:
        checks["postgres"] = auth.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("postgres not ready: %s", exc)
        checks["postgres"] = False
    ready = all(checks.values())
    return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)


@app.get("/v1/capabilities")
def capabilities_endpoint() -> dict[str, Any]:
    """Advertise supported protocols, ceilings, and optional features for client negotiation."""
    settings = app.state.settings
    limiter: RateLimiter = app.state.limiter
    return capabilities(
        max_batch_size=getattr(limiter, "burst", 0) or settings.rate_limit_burst,
        max_span_bytes=settings.max_span_bytes,
    )


@app.post("/v1/otlp/traces")
async def ingest_otlp_traces(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """OTLP/HTTP JSON fallback: translate ExportTraceServiceRequest → native spans, then ingest.

    Lets any OpenTelemetry SDK ship gen_ai spans without the ai-tally SDK. Tenant comes from the
    api key (auth on) or the ``X-Tenant-Id`` header (auth off, local dev).
    """
    otlp = await request.json()
    spans = otlp_traces_to_spans(otlp)
    tenant = request.headers.get("x-tenant-id", "")
    batch = _parse_batch(
        {"tenant_id": tenant, "sdk_version": "otlp-http", "resource_spans": spans}
    )
    return await _run_pipeline(batch, authorization)


@app.post("/v1/events")
async def ingest_events(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """CDP-shape event ingest convenience endpoint (CTO-105).

    Accepts a JSON body shaped like::

        {"events": [{"event_name": "...", "user_id_hash": "...", "occurred_at_ns": ..., ...}, ...]}

    or a single event object. Internally wraps the events into a zero-span
    :class:`BatchRequest` and runs the same pipeline as ``/v1/batches`` so the
    same auth / rate-limit / idempotency / write path applies. The chatbot
    demo's helper module is the first caller; SDKs that already batch spans +
    events use ``/v1/batches`` directly.
    """
    payload = await request.json()
    raw_events = payload.get("events") if isinstance(payload, dict) else None
    if raw_events is None and isinstance(payload, dict) and "event_name" in payload:
        raw_events = [payload]
    if not isinstance(raw_events, list) or not raw_events:
        raise HTTPException(status_code=422, detail="body must contain a non-empty 'events' list")

    tenant_id = (
        payload.get("tenant_id")
        if isinstance(payload, dict) and payload.get("tenant_id")
        else x_tenant_id or ""
    )
    events: list[BusinessEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="each event must be an object")
        try:
            events.append(
                BusinessEvent(
                    business_event_id=str(raw.get("business_event_id") or uuid7()),
                    event_name=str(raw["event_name"]),
                    user_id_hash=str(raw["user_id_hash"]),
                    occurred_at_ns=int(raw.get("occurred_at_ns") or time.time_ns()),
                    value_amount_micro=raw.get("value_amount_micro"),
                    value_currency=str(raw.get("value_currency") or "USD"),
                    value_type=str(raw.get("value_type") or "count"),
                    source=str(raw.get("source") or "cdp"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"malformed event: {exc}") from exc

    batch = BatchRequest(
        tenant_id=tenant_id,
        sdk_version="events-v1",
        resource_spans=[],
        business_events=events,
        batch_id=str(payload.get("batch_id") or uuid7()) if isinstance(payload, dict) else uuid7(),
    )
    return await _run_pipeline(batch, authorization)


@app.post("/v1/batches")
async def ingest_batch(
    request: Request,
    protocol_version: str | None = Header(default=None, alias="X-Ingest-Protocol"),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    payload = await request.json()
    # --- version negotiation: an unrecognized explicit protocol is a clean 400, not a guess. ---
    negotiated = negotiate(protocol_version)
    if negotiated is None:
        raise _error(
            400,
            ErrorCode.INVALID_SCHEMA,
            f"unsupported ingest protocol '{protocol_version}'; supported: {list(SUPPORTED_PROTOCOLS)}",
        )
    batch = _parse_batch(payload)
    return await _run_pipeline(batch, authorization)


async def _run_pipeline(batch: BatchRequest, authorization: str | None) -> JSONResponse:
    settings = app.state.settings
    store: ClickHouseStore = app.state.store
    auth: ApiKeyAuth = app.state.auth
    catalog = app.state.catalog
    idempotency: IdempotencyCache = app.state.idempotency
    limiter: RateLimiter = app.state.limiter

    claimed_tenant = batch.tenant_id

    # --- auth: resolve tenant + scope ---
    if settings.require_api_key:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _error(401, ErrorCode.UNAUTHENTICATED, "missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        result = auth.authenticate(token)
        if result is None:
            raise _error(401, ErrorCode.UNAUTHENTICATED, "invalid or revoked api key")
        if not result.can_write:
            raise _error(403, ErrorCode.FORBIDDEN_SCOPE, f"scope '{result.scope}' cannot write spans")
        # A key is tenant-bound: a body claiming a *different* tenant is refused, never silently
        # re-tagged. (Empty/own tenant in the body is fine.)
        if claimed_tenant and claimed_tenant != result.tenant_id:
            raise _error(403, ErrorCode.TENANT_MISMATCH, "key is not bound to the requested tenant")
        batch.tenant_id = result.tenant_id  # the key's tenant is authoritative
    elif not batch.tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id required when auth is disabled")

    # --- rate limit + monthly quota (one unit per span) ---
    decision = limiter.check(batch.tenant_id, max(1, len(batch.resource_spans)))
    if not decision.allowed:
        # Both RATE_LIMITED and QUOTA_EXCEEDED are 429 so a conformant client backs off honoring
        # Retry-After; the body's error.code lets it distinguish a transient cap from a spent quota.
        retry_after_s = max(1, round(decision.retry_after_s))
        return JSONResponse(
            {
                "batch_id": batch.batch_id,
                "status": Status.REJECTED.value,
                "error": {"code": decision.code.value if decision.code else "", "message": decision.message},
                "retry_after_ms": decision.retry_after_ms,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after_s)},
        )

    # --- idempotency: replayed batch returns the original response ---
    cached = idempotency.check_or_store(batch)
    if cached is not None:
        return JSONResponse(_response_dict(cached, replayed=True), status_code=200)

    batch = batch.deduplicated()
    server_recv_ns = time.time_ns()

    # --- backpressure: under load, shed the batch's overflow (retryable) + tighten client hints ---
    backpressure: Backpressure = app.state.backpressure
    shed = backpressure.evaluate(getattr(app.state, "in_flight", 1), len(batch.resource_spans))
    hints = shed.hints
    partial_errors: list[PartialError] = []
    if shed.overloaded and shed.keep < len(batch.resource_spans):
        overflow = batch.resource_spans[shed.keep :]
        batch.resource_spans = batch.resource_spans[: shed.keep]
        for index, span in enumerate(overflow, start=shed.keep):
            item_id = span_item_id(span, index) if isinstance(span, dict) else f"#{index}"
            partial_errors.append(
                PartialError(item_id=item_id, code=ErrorCode.RATE_LIMITED.value, message="shed under load")
            )

    # --- validate (per item) + enrich + map spans ---
    validator: SpanValidator = app.state.validator
    metering: UsageRollup = app.state.metering
    rows: list[tuple[object, ...]] = []
    # CTO-237: candidates for the opt-in replay corpus, collected in lockstep with the written
    # rows. Building these is cheap and body-free (token counts + resolved-context metadata only);
    # the tenant's replay config gates whether any are actually sampled/persisted, in
    # capture_replay_samples_for_batch below.
    replay_candidates: list[SampleCandidate] = []
    drift_count = 0
    for index, span in enumerate(batch.resource_spans):
        item_id = span_item_id(span, index) if isinstance(span, dict) else f"#{index}"
        verdict = validator.validate(span)
        if not verdict.accepted:
            partial_errors.append(
                PartialError(item_id=item_id, code=verdict.rejection.value, message=verdict.message)
            )
            continue
        for flag in verdict.flags:  # accepted-but-flagged (e.g. UNKNOWN_FEATURE_TAG)
            partial_errors.append(PartialError(item_id=item_id, code=flag.value, message=""))
        assert isinstance(span, dict)  # narrowed by verdict.accepted
        result = enrich_cost(span, catalog, tenant_id=batch.tenant_id)
        if result.drift_exceeded:
            drift_count += 1
        client_ts = span.get("timestamp_ns")
        client_ts_ns = client_ts if isinstance(client_ts, int) else batch.client_send_ts_ns
        skew = assess(client_ts_ns, server_recv_ns)
        # Meter at HEAD — before the analytics sampling decision — so the billable trace count is
        # exact regardless of sample_rate (CTO-84/85). Drops/sampling must never lower the bill.
        trace_id = span.get("TraceId") or span.get("trace_id")
        feature_tag = result.attributes.get(GenAI.FEATURE_TAG)
        metering.record_span(
            batch.tenant_id,
            trace_id=trace_id if isinstance(trace_id, str) else None,
            feature_tag=feature_tag if isinstance(feature_tag, str) else None,
            ts_ns=skew.effective_ts_ns,
        )
        rows.append(
            span_to_row(
                result.attributes,
                tenant_id=batch.tenant_id,
                effective_ts_ns=skew.effective_ts_ns,
                sample_rate=batch.sampling.head_sample_rate,
            )
        )
        # CTO-237: mirror this accepted span into a replay SampleCandidate. The envelope carries
        # ONLY token counts + resolved-context metadata, never a prompt/completion body (the SDK
        # never sends one and the validator rejects bodies), so the no-bodies-in-telemetry posture
        # is preserved; build_payloads still PII-scrubs the envelope before it reaches object
        # storage. The mock candidate client replays purely off these token counts.
        span_id = span.get("SpanId") or span.get("span_id")
        input_tokens = _as_int(result.attributes.get(GenAI.USAGE_INPUT_TOKENS))
        output_tokens = _as_int(result.attributes.get(GenAI.USAGE_OUTPUT_TOKENS))
        provider = result.attributes.get(GenAI.SYSTEM)
        model = result.attributes.get(GenAI.RESPONSE_MODEL) or result.attributes.get(
            GenAI.REQUEST_MODEL
        )
        replay_candidates.append(
            SampleCandidate(
                trace_id=trace_id if isinstance(trace_id, str) else "",
                span_id=span_id if isinstance(span_id, str) else "",
                feature_tag=feature_tag if isinstance(feature_tag, str) else "untagged",
                real_provider=str(provider) if provider else "",
                real_model=str(model) if model else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                envelope={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "real_provider": str(provider) if provider else "",
                    "real_model": str(model) if model else "",
                    "feature_tag": feature_tag if isinstance(feature_tag, str) else "untagged",
                    "context_fidelity": "resolved-context",
                },
            )
        )

    # If every span was rejected (and there were spans), nothing to write — REJECTED, no retry.
    rejected_only = bool(batch.resource_spans) and not rows
    if rejected_only:
        resp = BatchResponse(
            batch_id=batch.batch_id,
            status=Status.REJECTED,
            partial_errors=partial_errors,
            server_hints=hints,
        )
        idempotency.record(batch, resp)
        return JSONResponse(_response_dict(resp), status_code=422)

    # --- write ---
    buffer: AsyncIngestBuffer | None = app.state.ingest_buffer
    if buffer is not None:
        # Buffered path (CTO-37): hand spans to the burst buffer (drained to ClickHouse off the hot
        # path) and ack immediately, so a burst or a slow ClickHouse never yields a 5xx. Overflow past
        # the buffer's high-water mark is shed as retryable partial errors — backpressure, not failure.
        produced = buffer.produce_rows(batch.tenant_id, rows)
        accepted = produced.accepted
        for i in range(produced.rejected):
            partial_errors.append(
                PartialError(
                    item_id=f"#buffer-overflow-{i}",
                    code=ErrorCode.RATE_LIMITED.value,
                    message="ingest buffer at capacity; retry",
                )
            )
        # Business events / identity links are low-volume metadata, not the burst hot path, so they
        # still write synchronously; a ClickHouse outage on these is surfaced as retryable.
        try:
            store.insert_business_events(batch.tenant_id, batch.business_events)
            store.insert_identity_links(batch.tenant_id, batch.identity_links)
        except Exception:  # noqa: BLE001 - keep the gateway alive
            logger.exception("clickhouse insert (events/links) failed")
            resp = BatchResponse(batch_id=batch.batch_id, status=Status.RETRY, server_hints=hints)
            idempotency.record(batch, resp)
            return JSONResponse(_response_dict(resp), status_code=503)
    else:
        try:
            accepted = store.insert_spans(rows)
            store.insert_business_events(batch.tenant_id, batch.business_events)
            store.insert_identity_links(batch.tenant_id, batch.identity_links)
        except Exception:  # noqa: BLE001 - surface as retryable, keep the gateway alive
            logger.exception("clickhouse insert failed")
            resp = BatchResponse(batch_id=batch.batch_id, status=Status.RETRY, server_hints=hints)
            idempotency.record(batch, resp)
            return JSONResponse(_response_dict(resp), status_code=503)

    # --- replay capture (CTO-237): populate the opt-in replay corpus from this batch ---
    # Runs AFTER the ClickHouse write and AFTER per-item validation (the no-bodies/PII guard), so a
    # sample is only ever captured for a span that was accepted and scrubbed. Gated inside
    # capture_replay_samples_for_batch on the tenant's replay config (default OFF): a non-opted-in
    # tenant captures nothing. Best-effort: a capture or Postgres-config hiccup must never fail an
    # already-accepted ingest, so it is wrapped and swallowed with a logged exception.
    if replay_candidates:
        try:
            capture_replay_samples_for_batch(
                batch.tenant_id,
                replay_candidates,
                store=store,
            )
        except Exception:  # noqa: BLE001 - capture is best-effort; never fail accepted ingest
            logger.exception("replay capture failed for batch %s", batch.batch_id)

    if drift_count:
        logger.info("catalog drift on %d/%d spans (batch %s)", drift_count, len(rows), batch.batch_id)

    # Some items rejected/flagged but others written → PARTIAL; otherwise clean ACCEPTED.
    fatal = [e for e in partial_errors if e.code != ErrorCode.UNKNOWN_FEATURE_TAG.value]
    status = Status.PARTIAL if fatal else Status.ACCEPTED
    resp = BatchResponse(
        batch_id=batch.batch_id,
        status=status,
        accepted_spans=accepted,
        partial_errors=partial_errors,
        server_hints=hints,
    )
    idempotency.record(batch, resp)
    return JSONResponse(_response_dict(resp), status_code=200)


@app.get("/v1/usage")
def get_usage(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    period: str | None = None,
) -> JSONResponse:
    """Current-period (or ``?period=YYYY-MM``) usage vs. plan limit for the caller's tenant.

    Consumed by the dashboard ("usage vs. plan limit") and billing (CTO-86). Tenant resolves from
    the API key when auth is on, else from the ``X-Tenant-Id`` header (local dev).
    """
    settings = app.state.settings
    auth: ApiKeyAuth = app.state.auth
    metering: UsageRollup = app.state.metering

    if settings.require_api_key:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        tenant_id = auth.tenant_for_key(token)
        if tenant_id is None:
            raise HTTPException(status_code=403, detail="invalid api key")
    else:
        tenant_id = x_tenant_id
        if not tenant_id:
            raise HTTPException(status_code=422, detail="X-Tenant-Id required when auth is disabled")

    record = metering.usage(tenant_id, period)
    return JSONResponse(record.as_dict(), status_code=200)


def _resolve_tenant_for_control_plane(
    authorization: str | None, x_tenant_id: str | None
) -> str:
    """Shared tenant-resolution for read/write control-plane endpoints.

    Same pattern as :func:`get_usage`: bearer key when auth is on, ``X-Tenant-Id`` header in dev.
    Refuses ambiguity so the caller can never accidentally cross tenants.
    """
    settings = app.state.settings
    auth: ApiKeyAuth = app.state.auth
    if settings.require_api_key:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        tenant_id = auth.tenant_for_key(token)
        if tenant_id is None:
            raise HTTPException(status_code=403, detail="invalid api key")
        return tenant_id
    if not x_tenant_id:
        raise HTTPException(status_code=422, detail="X-Tenant-Id required when auth is disabled")
    return x_tenant_id


@app.exception_handler(TenantNotFoundError)
def _tenant_not_found_handler(_request: Request, exc: TenantNotFoundError) -> JSONResponse:
    """Map an unresolved tenant identifier onto a clean 404 (CTO-201).

    Control-plane tables key on ``tenants.id`` while the dashboard and local dev identify a tenant
    by NAME. The stores fold the name onto the UUID via ``resolve_tenant_uuid`` and raise this when
    the name matches no row. Without this handler that raise would surface as an opaque 500 with a
    driver-shaped body; here it becomes the same ``{"detail": ...}`` 404 the endpoints that catch it
    inline already return. Endpoints that translate it themselves still win, so this only backstops
    the stores whose 404 is not mapped inline.
    """
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/v1/tenant/connectors")
def list_tenant_connectors(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """List declared cost-layer connectors for the caller's tenant (CTO-107).

    The dashboard consumes this to decide whether the "Partial data" banner should fire: only
    *enabled* layers count as a real gap when they report zero. Layers the tenant never enabled
    don't appear in the response and don't contribute to partiality.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantConnectorStore = app.state.tenant_connectors
    rows = store.list(tenant_id)
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "connectors": [r.as_dict() for r in rows],
            "enabled_layers": [r.layer for r in rows if r.enabled],
        },
        status_code=200,
    )


@app.post("/v1/tenant/connectors")
async def set_tenant_connector(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Enable or disable one cost-layer connector for the caller's tenant.

    Body: ``{"layer": "vector", "enabled": true, "notes": "optional"}``. Idempotent — re-enabling an
    already-enabled connector is a no-op, disabling an absent one stamps a tombstone row.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    layer = body.get("layer")
    enabled = body.get("enabled")
    notes = body.get("notes")
    if not isinstance(layer, str) or layer not in ALLOWED_LAYERS:
        raise HTTPException(
            status_code=422, detail=f"layer must be one of {sorted(ALLOWED_LAYERS)}"
        )
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled must be a boolean")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(status_code=422, detail="notes must be a string when provided")
    store: TenantConnectorStore = app.state.tenant_connectors
    row = store.set(tenant_id, layer, enabled=enabled, notes=notes)
    return JSONResponse({"tenant_id": tenant_id, "connector": row.as_dict()}, status_code=200)


@app.get("/v1/tenant/cost-connectors")
def list_cost_connectors(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Configured cloud cost connectors for the caller's tenant (CTO-176).

    Safe view only: every ``*_ref`` returned is a secret-manager REFERENCE, which is all the column
    ever holds. No raw credential exists to leak here.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    admin: CostConnectorAdmin = app.state.cost_connector_admin
    try:
        rows = admin.list_configs(tenant_id)
    except Exception as exc:  # noqa: BLE001 - control plane read, degrade rather than 500 the page
        logger.warning("cost connector list failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="control plane unavailable") from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "configs": [r.as_dict() for r in rows]}, status_code=200
    )


@app.post("/v1/tenant/cost-connectors")
async def upsert_cost_connector(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Create or replace one cloud cost connector's config.

    Body: ``{"connector": "aws_cost_explorer", "credentials_ref": "arn:...", ...}``. Per-connector
    required fields are enforced in :mod:`gateway.connectors.config_admin`, which also rejects
    anything shaped like a raw credential before it can reach Postgres.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    connector = body.get("connector")
    if not isinstance(connector, str) or connector not in ALL_CONNECTORS:
        raise HTTPException(
            status_code=422, detail=f"connector must be one of {sorted(ALL_CONNECTORS)}"
        )
    admin: CostConnectorAdmin = app.state.cost_connector_admin
    try:
        result = admin.upsert(tenant_id, connector, body)
    except ConfigError as exc:
        # Validation messages are authored for the operator and carry no secret material.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, **result}, status_code=200)


@app.delete("/v1/tenant/cost-connectors/{connector}")
def delete_cost_connector(
    connector: str,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Disconnect one cloud cost connector. Idempotent: deleting an absent row is a 200."""
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    admin: CostConnectorAdmin = app.state.cost_connector_admin
    try:
        deleted = admin.delete(tenant_id, connector)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "connector": connector, "deleted": deleted}, status_code=200
    )


@app.get("/v1/tenant/guardrails")
def list_tenant_guardrails(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """List guardrail rules for the caller's tenant (CTO-116).

    The SDK polls this on its config-refresh interval; the dashboard renders the same payload.
    Rules in 'shadow' state are evaluated and observed but never alter agent behavior — that's the
    safe staging step before flipping to 'enabled'.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantGuardrailStore = app.state.tenant_guardrails
    rules = store.list(tenant_id)
    return JSONResponse({
        "tenant_id": tenant_id,
        "rules": [r.as_dict() for r in rules],
    })


@app.post("/v1/tenant/guardrails")
async def upsert_tenant_guardrail(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Upsert a guardrail rule. Idempotent on client-supplied change_id (CTO-116).

    Body: ``{rule_id, kind, params, state, change_id, actor?, notes?}``. Replaying the same
    change_id is a no-op (returns the existing rule unchanged).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    rule_id = body.get("rule_id")
    kind = body.get("kind")
    state = body.get("state")
    params = body.get("params") or {}
    change_id = body.get("change_id")
    if not isinstance(rule_id, str) or not rule_id:
        raise HTTPException(status_code=422, detail="rule_id required")
    if kind not in GUARDRAIL_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {sorted(GUARDRAIL_KINDS)}"
        )
    if state not in GUARDRAIL_STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of {sorted(GUARDRAIL_STATES)}"
        )
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="params must be an object")
    if not isinstance(change_id, str) or not change_id:
        raise HTTPException(status_code=422, detail="change_id required (uuid)")
    store: TenantGuardrailStore = app.state.tenant_guardrails
    try:
        rule = store.upsert(
            tenant_id,
            rule_id,
            kind=kind,
            params=params,
            state=state,
            change_id=change_id,
            actor=body.get("actor"),
            notes=body.get("notes"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "rule": rule.as_dict()})


@app.get("/v1/tenant/guardrails/audit")
def list_tenant_guardrail_audit(
    rule_id: str | None = None,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Recent guardrail rule changes for the caller's tenant (CTO-116)."""
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantGuardrailStore = app.state.tenant_guardrails
    changes = store.audit(tenant_id, rule_id=rule_id)
    return JSONResponse({
        "tenant_id": tenant_id,
        "rule_id": rule_id,
        "changes": [c.as_dict() for c in changes],
    })


@app.get("/v1/tenant/feature-value-events")
def list_tenant_feature_value_events(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """List the caller tenant's feature -> value-event mappings (CTO-140).

    The /features page reads this to overlay the configured value event onto each feature row and
    to decide whether the onboarding "Finish setup" banner should still show.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantFeatureValueEventStore = app.state.tenant_feature_value_events
    events = store.list(tenant_id)
    return JSONResponse({
        "tenant_id": tenant_id,
        "value_events": [e.as_dict() for e in events],
    })


@app.post("/v1/tenant/feature-value-events")
async def upsert_tenant_feature_value_event(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Pin a value event to a feature. Idempotent on client-supplied change_id (CTO-140).

    Body: ``{feature_tag, event_name, change_id, actor?, notes?}``. Replaying the same change_id is
    a no-op (returns the existing mapping unchanged).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    feature_tag = body.get("feature_tag")
    event_name = body.get("event_name")
    change_id = body.get("change_id")
    notes = body.get("notes")
    if not isinstance(feature_tag, str) or not feature_tag:
        raise HTTPException(status_code=422, detail="feature_tag required")
    if not isinstance(event_name, str) or not event_name:
        raise HTTPException(status_code=422, detail="event_name required")
    if not isinstance(change_id, str) or not change_id:
        raise HTTPException(status_code=422, detail="change_id required (uuid)")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(status_code=422, detail="notes must be a string when provided")
    store: TenantFeatureValueEventStore = app.state.tenant_feature_value_events
    try:
        event = store.upsert(
            tenant_id,
            feature_tag,
            event_name=event_name,
            change_id=change_id,
            actor=body.get("actor"),
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "value_event": event.as_dict()})


@app.delete("/v1/tenant/feature-value-events")
async def delete_tenant_feature_value_event(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Remove a feature's value-event mapping. Idempotent on change_id (CTO-140).

    Body: ``{feature_tag, change_id, actor?}``. Deleting an absent mapping (or replaying a change_id)
    is a no-op that still returns 200.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    feature_tag = body.get("feature_tag")
    change_id = body.get("change_id")
    if not isinstance(feature_tag, str) or not feature_tag:
        raise HTTPException(status_code=422, detail="feature_tag required")
    if not isinstance(change_id, str) or not change_id:
        raise HTTPException(status_code=422, detail="change_id required (uuid)")
    store: TenantFeatureValueEventStore = app.state.tenant_feature_value_events
    removed = store.delete(
        tenant_id, feature_tag, change_id=change_id, actor=body.get("actor")
    )
    return JSONResponse({"tenant_id": tenant_id, "feature_tag": feature_tag, "removed": removed})


@app.post("/v1/tenant/stripe/connect")
async def connect_tenant_stripe(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Persist a tenant's Stripe webhook signing secret (CTO-110).

    Body: ``{"webhook_secret": "whsec_...", "stripe_account_id": "acct_..." (optional)}``.
    Idempotent: pasting the same secret twice is a no-op on the audit log. The response carries
    a *fingerprint* of the secret (last 4 chars) so the dashboard can show "connected" — the raw
    secret is never re-exposed.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    secret = body.get("webhook_secret")
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise HTTPException(
            status_code=422,
            detail="webhook_secret must be a Stripe signing secret starting with 'whsec_'",
        )
    account_id = body.get("stripe_account_id")
    if account_id is not None and not isinstance(account_id, str):
        raise HTTPException(status_code=422, detail="stripe_account_id must be a string")
    store: TenantStripeStore = app.state.tenant_stripe
    try:
        cfg = store.connect(tenant_id, secret, stripe_account_id=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "stripe": cfg.as_safe_dict()}, status_code=200)


@app.get("/v1/tenant/stripe")
def get_tenant_stripe(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Read the safe (no-secret) view of a tenant's Stripe config — used by the connectors tile."""
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantStripeStore = app.state.tenant_stripe
    cfg = store.get(tenant_id)
    return JSONResponse(
        {"tenant_id": tenant_id, "stripe": cfg.as_safe_dict() if cfg else None},
        status_code=200,
    )


@app.get("/v1/tenant/integrations/status")
def list_tenant_integration_status(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Per-tenant third-party integration run status (CTO-117).

    Returns one entry per integration the tenant has had at least one run for. The dashboard
    merges this against its static catalog of supported third-party integrations and renders
    catalog-entries-without-a-row as "Not connected" (the honest default for fresh tenants).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantIntegrationStore = app.state.tenant_integrations
    rows = store.get_status(tenant_id)
    return JSONResponse(
        {"tenant_id": tenant_id, "integrations": [r.as_dict() for r in rows]},
        status_code=200,
    )


@app.get("/v1/tenant/reconciliation/status")
def get_tenant_reconciliation_status(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Latest reconciler run for the caller's tenant (CTO-139).

    Drives the /features "Attribution diagnostics" card: late-arrival event count, median lateness,
    and how long ago the reconciler last ran. ``run`` is ``None`` when no pass has ever run for the
    tenant — the honest first-render state, which the web fn turns into a null so the route falls
    back to its mock.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: ReconciliationStore = app.state.reconciliation
    run = store.get_latest(tenant_id)
    return JSONResponse(
        {"tenant_id": tenant_id, "run": run.as_dict() if run is not None else None},
        status_code=200,
    )


@app.post("/v1/tenant/account-lookup")
async def lookup_account_id(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Plaintext account id -> ``AccountIdHash``, for the cost-per-customer search box (CTO-185).

    WHY. The cost-per-customer tab groups spend by ``AccountIdHash`` (CTO-180), and an account
    label is optional per tenant by design, so an unlabelled account renders as a 64-character
    hex string. There is no reverse map from hash to id and there deliberately never will be, so
    without this endpoint an operator who knows their customer as ``acme-corp`` cannot locate that
    row at all and the tab is a list of opaque strings. This is the forward direction, computed on
    demand, which is the only direction a one-way hash has.

    Body: ``{"account_id": "acme-corp"}``. Response carries ``account_id_hash`` (the best match)
    plus every candidate in ``hashes``.

    Two properties this endpoint exists to hold:

    * **Parity with the emitting path.** The digest comes from the SDK's own
      :class:`tally.hmac_keys.HmacKeyRegistry` under the tenant's active key version, the same
      derivation ``hash_customer_email`` and ``build_hasher`` already use. A hash computed any
      other way would be well-formed and match nothing.

    * **The plaintext is transient.** It is used for one HMAC call and then dropped. It is never
      written to a row, never logged, and never quoted back in an error: every rejection message
      in :mod:`gateway.account_lookup` describes the problem without echoing the value. That is
      also why this is a POST with a body rather than a GET with a query string, which would put
      a customer identifier into access logs.

    An account id nobody has ever emitted is NOT an error. It returns 200 with a perfectly valid
    hash that simply matches no rows, and the tab renders "no spend recorded for this account".
    Answering 404 here would leak the difference between an account this tenant has and one it
    does not, and would also make a genuine typo indistinguishable from a genuinely idle customer.

    ``hashes`` holds one entry per spelling of the tenant identifier (see
    :mod:`gateway.tenant_identity`): the tenant name and the ``tenants.id`` UUID derive different
    HMAC keys, so callers should match spans against the whole set rather than assume a spelling.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        # Deliberately no `{exc}` interpolation, unlike the neighbouring endpoints: a decoder
        # message can quote the offending bytes, and those bytes are the account id.
        raise HTTPException(status_code=422, detail="body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    try:
        account_id = normalize_account_id(body.get("account_id"))
    except AccountLookupError as exc:
        # Safe by construction: AccountLookupError messages never contain the submitted value.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    resolver: TenantIdentityResolver = app.state.tenant_identity
    registry: HmacKeyRegistry = app.state.hmac_registry
    hashes = hash_account_id(registry, resolver.key_forms(tenant_id), account_id)
    if not hashes:
        raise HTTPException(status_code=422, detail="tenant id must be non-empty")
    # No log line here, at any level. The only interesting thing to report would be what was
    # looked up, and that is precisely the thing that must not be recorded.
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "account_id_hash": hashes[0].account_id_hash,
            "key_version": hashes[0].key_version,
            "hashes": [h.as_dict() for h in hashes],
        },
        status_code=200,
    )


def _account_label_body(body: object) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return body


def _resolve_label_target_hashes(body: dict, tenant_id: str) -> list[str]:
    """Which ``AccountIdHash`` values a label write or delete applies to.

    Two ways to name the account, and the second one is the whole reason this helper exists:

    * ``account_id_hash`` addresses exactly one digest. Use this when the caller already holds a
      hash, typically straight out of a row on the tab. One hash in, one row touched.

    * ``account_id`` is the plaintext, and it expands to EVERY digest the tenant could have emitted
      under. For HMAC the tenant identifier is key material (see :mod:`gateway.tenant_identity`),
      so ``local-dev`` and its UUID derive two unrelated key spaces and therefore two different
      hashes for the same account. Writing under only one of them would label the account for spans
      ingested through one door and leave the other door showing raw hex, which reads as a rename
      that half applied. So we reuse the exact set ``/v1/tenant/account-lookup`` returns and write
      under all of it.

    The plaintext is treated the way B6 treats it: used to compute digests and then dropped. It is
    never stored (only hashes reach the table), never logged, and never echoed in an error.
    """
    supplied_hash = body.get("account_id_hash")
    supplied_id = body.get("account_id")
    if (supplied_hash is None) == (supplied_id is None):
        raise HTTPException(
            status_code=422,
            detail="exactly one of account_id_hash or account_id required",
        )
    if supplied_hash is not None:
        try:
            return [normalize_account_id_hash(supplied_hash)]
        except AccountLabelError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        account_id = normalize_account_id(supplied_id)
    except AccountLookupError as exc:
        # Safe by construction: these messages never contain the submitted value.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    resolver: TenantIdentityResolver = app.state.tenant_identity
    registry: HmacKeyRegistry = app.state.hmac_registry
    hashes = hash_account_id(registry, resolver.key_forms(tenant_id), account_id)
    if not hashes:
        raise HTTPException(status_code=422, detail="tenant id must be non-empty")
    return [h.account_id_hash for h in hashes]


@app.get("/v1/tenant/account-labels")
def list_tenant_account_labels(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Every account label this tenant has set (CTO-186).

    The cost-per-customer tab fetches this once and joins it in memory against a page of account
    rows. An account with no entry here is not missing data: labels are optional per account by
    design, and the tab falls back to a shortened hash. A tenant that wants no customer names in
    our system sets none and everything still works.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantAccountLabelStore = app.state.tenant_account_labels
    try:
        labels = store.list(tenant_id)
    except AccountLabelTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({
        "tenant_id": tenant_id,
        "labels": [label.as_dict() for label in labels],
    })


@app.post("/v1/tenant/account-labels")
async def upsert_tenant_account_label(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Set or rename an account's label (CTO-186).

    Body: ``{"label": "Acme Corp", "account_id_hash": "..."}`` or
    ``{"label": "Acme Corp", "account_id": "acme-corp"}``. Exactly one of the two identifiers.

    Setting and renaming are the same call: the write is an upsert on
    ``(tenant_id, account_id_hash)``, so the caller never has to know whether a label already
    exists and two concurrent writers cannot produce two rows for one account. There is no
    ``change_id`` idempotency token here, unlike the feature-value-event control plane, because a
    label is last-write-wins by nature: replaying the same body is already a no-op beyond
    ``updated_at``.

    The label is written to Postgres and joined at render time. It is never stamped onto a span,
    so ClickHouse never holds a customer name. See ``db/postgres/0023_tenant_account_labels.sql``.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        # No `{exc}` interpolation: a decoder message can quote the offending bytes, and those
        # bytes may be an account id or a customer name.
        raise HTTPException(status_code=422, detail="body is not valid JSON") from exc
    body = _account_label_body(raw)
    try:
        label = normalize_label(body.get("label"))
    except AccountLabelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    hashes = _resolve_label_target_hashes(body, tenant_id)

    store: TenantAccountLabelStore = app.state.tenant_account_labels
    try:
        rows = store.upsert_many(tenant_id, hashes, label=label)
    except AccountLabelTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccountLabelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({
        "tenant_id": tenant_id,
        "label": rows[0].as_dict(),
        "labels": [row.as_dict() for row in rows],
    })


@app.delete("/v1/tenant/account-labels")
async def delete_tenant_account_label(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Remove an account's label, reverting it to its hash on the tab (CTO-186).

    Body: ``{"account_id_hash": "..."}`` or ``{"account_id": "acme-corp"}``.

    This really deletes the row. It is the escape hatch for a tenant who decides they want no
    customer names in our system, so a tombstone or an audit snapshot would keep the name on disk
    after they asked us to forget it and make the escape hatch a fiction.

    Deleting an already-unlabelled account returns 200 with ``removed: false`` rather than 404. The
    end state the caller asked for is the end state they get, and a double-click is not an error.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="body is not valid JSON") from exc
    body = _account_label_body(raw)
    hashes = _resolve_label_target_hashes(body, tenant_id)

    store: TenantAccountLabelStore = app.state.tenant_account_labels
    try:
        removed = store.delete_many(tenant_id, hashes)
    except AccountLabelTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccountLabelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({
        "tenant_id": tenant_id,
        "account_id_hashes": hashes,
        "removed": removed > 0,
        "rows_removed": removed,
    })


@app.get("/v1/tenant/allocation-config")
def get_tenant_allocation_config(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """The shared-cost allocation rule in force for the caller's tenant (CTO-193).

    The response ALWAYS names a usable rule, and separately says whether anyone chose it:

    * ``allocation_rule``: what /cost-per-customer must apply. Never null.
    * ``configured``: false when the tenant has no row, i.e. the rule above is the default.
    * ``config``: the stored row, or null. Carries ``updated_at`` / ``updated_by``.
    * ``available_rules``: every rule this deployment can apply, so a config surface does not
      have to hardcode the list and drift from the CHECK constraint.

    Splitting "which rule applies" from "did someone pick it" is the whole point of the shape. The
    page names the rule beside the column it produced, and "pro rata, the default" and "pro rata,
    chosen by finance in March" are different claims to put in front of a reader.

    A tenant with no ``tenants`` row is 404, not a silent default: that is a misrouted request, and
    inventing an allocation rule for a tenant we do not know is not a recovery.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantAllocationStore = app.state.tenant_allocation
    try:
        config = store.get(tenant_id)
    except AllocationTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "allocation_rule": config.allocation_rule if config else DEFAULT_ALLOCATION_RULE,
            "configured": config is not None,
            "default_rule": DEFAULT_ALLOCATION_RULE,
            "available_rules": list(ALLOCATION_RULES),
            "config": config.as_dict() if config else None,
        },
        status_code=200,
    )


@app.post("/v1/tenant/allocation-config")
async def upsert_tenant_allocation_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Set the tenant's shared-cost allocation rule. Idempotent on ``change_id`` (CTO-193).

    Body: ``{"allocation_rule": "pro_rata_direct" | "even_split", "change_id": "<uuid>",
    "updated_by": "finance@acme.test"?}``.

    An unknown rule is a 422 rather than a fallback to the default. Storing one rule and applying
    another would leave the page naming a rule that did not produce its numbers, which is exactly
    the invisible assumption this config exists to remove.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    change_id = body.get("change_id")
    if not isinstance(change_id, str) or not change_id.strip():
        raise HTTPException(status_code=422, detail="change_id required (uuid)")

    store: TenantAllocationStore = app.state.tenant_allocation
    try:
        rule = normalize_rule(body.get("allocation_rule"))
        updated_by = normalize_updated_by(body.get("updated_by"))
        config = store.upsert(
            tenant_id, rule, change_id=change_id.strip(), updated_by=updated_by
        )
    except AllocationTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AllocationConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "allocation_rule": config.allocation_rule,
            "configured": True,
            "default_rule": DEFAULT_ALLOCATION_RULE,
            "available_rules": list(ALLOCATION_RULES),
            "config": config.as_dict(),
        },
        status_code=200,
    )


@app.get("/v1/tenant/budgets")
def list_tenant_budgets(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Every budget this tenant has set (CTO-205, F1).

    The response separates "what is stored" from "is anything stored":

    * ``budgets``: the rows, each with ``amount_micro`` as integer micro-USD. Never dollars.
    * ``configured``: false when the tenant has no budget at all. THIS IS THE NORMAL STATE and
      every tenant on this system is in it today. It is not an error and it is not a budget of
      zero: downstream must render "no budget set" and omit the variance rather than reporting the
      tenant as infinitely over. A stored ``amount_micro`` of 0 is a different, deliberate claim.
    * ``available_periods`` / ``available_scope_kinds``: what this deployment can store, so a
      settings UI does not hardcode the lists and drift from the CHECK constraints.

    An unknown tenant is 404 rather than an empty list. A misrouted request is not a tenant who has
    set no budgets, and answering "no budget set" for a tenant we do not know would hide the bug.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantBudgetStore = app.state.tenant_budgets
    try:
        budgets = store.list(tenant_id)
    except BudgetTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "budgets": [b.as_dict() for b in budgets],
            "configured": bool(budgets),
            "available_periods": list(BUDGET_PERIODS),
            "available_scope_kinds": list(BUDGET_SCOPE_KINDS),
        },
        status_code=200,
    )


@app.post("/v1/tenant/budgets")
async def upsert_tenant_budget(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Create or edit one budget (CTO-205, F1).

    Body: ``{"budget_id": "research-agent-2026", "period": "month", "amount_micro": 30000000000,
    "scope_kind": "feature", "scope_value": "research-agent", "starts_on": "2026-01-01",
    "ends_on": null}``.

    Creating and editing are the same call, an upsert on ``(tenant_id, budget_id)``. There is no
    ``change_id`` idempotency token here, unlike the allocation-config control plane, because there
    is no audit log to append to and the write is last-write-wins by nature: replaying the same
    body is already a no-op beyond ``updated_at``.

    ``amount_micro`` is an INTEGER of micro-USD and a float is a 422, not a rounding. $30,000 is
    ``30000000000``. Every cost figure this budget will be compared against is already an integer
    of micro-USD, and admitting dollars-as-float at the boundary is how the two stop reconciling.

    **Overlap is a 409.** A budget that covers the same scope and period over an overlapping date
    range as an existing one is refused, and the response names the budget it collided with in
    ``conflicting_budget_id``. The alternative, resolving the ambiguity at read time, has no
    principled tie-break and would have to be reimplemented identically in the projection, the
    burn-down and the future breach alerts. See ``db/postgres/0026_tenant_budgets.sql``. To replace
    a budget, either POST the same ``budget_id`` (which edits in place) or close the old one by
    setting its ``ends_on`` first.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    store: TenantBudgetStore = app.state.tenant_budgets
    try:
        budget = store.upsert(
            tenant_id,
            budget_id=body.get("budget_id"),
            period=body.get("period"),
            amount_micro=body.get("amount_micro"),
            scope_kind=body.get("scope_kind"),
            scope_value=body.get("scope_value", ""),
            starts_on=body.get("starts_on"),
            ends_on=body.get("ends_on"),
        )
    except BudgetTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BudgetOverlapError as exc:
        # 409 rather than 422: the body is well-formed, it conflicts with state. The distinction
        # matters to a UI, which should offer to edit the colliding budget rather than re-validate
        # the form the user just filled in correctly.
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "conflicting_budget_id": exc.conflicting_budget_id,
            },
        ) from exc
    except BudgetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "budget": budget.as_dict()}, status_code=200)


@app.delete("/v1/tenant/budgets")
async def delete_tenant_budget(
    request: Request,
    budget_id: str | None = None,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Withdraw one budget, returning that scope to "no budget set" (CTO-205, F1).

    Takes ``budget_id`` as a query parameter or in a JSON body, because DELETE-with-a-body is
    awkward from several HTTP clients and this endpoint addresses a single named row.

    A real DELETE. A budget is the tenant's own statement of intent, so withdrawing it must not
    leave a row behind still claiming they intend it.

    Deleting an absent budget returns 200 with ``removed: false`` rather than 404, same as the
    account-labels control plane: the end state the caller asked for is the end state they get, and
    a double-click is not an error.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    if budget_id is None:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an absent body is normal when the query param was used
            body = None
        if isinstance(body, dict):
            budget_id = body.get("budget_id")
    store: TenantBudgetStore = app.state.tenant_budgets
    try:
        target = normalize_budget_id(budget_id)
        removed = store.delete(tenant_id, target)
    except BudgetTenantNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BudgetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "budget_id": target, "removed": removed},
        status_code=200,
    )


@app.post("/v1/stripe/webhook")
async def stripe_webhook(
    request: Request,
    tenant: str | None = None,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> JSONResponse:
    """Stripe webhook ingest (CTO-110).

    Route shape: ``POST /v1/stripe/webhook?tenant=<tenant_id>`` — Stripe can't add custom headers,
    so the tenant is encoded in the URL (the tenant's Stripe dashboard configures it once at
    connect time). Verification + idempotency + insert happens here; we ack 200 on every path
    that isn't a hard rejection so Stripe doesn't redeliver.

    Returns 200 in well under 1s on the happy path: signature check is one HMAC, idempotency is
    an in-memory set probe, the insert is the same low-volume CH path business_events already
    uses for the SDK.
    """
    if not tenant:
        raise HTTPException(status_code=422, detail="missing ?tenant= query param")
    body_bytes = await request.body()

    stripe_store: TenantStripeStore = app.state.tenant_stripe
    cfg = stripe_store.get(tenant)
    if cfg is None or not cfg.is_active:
        # 401: Stripe will retry, but the tenant needs to connect first.
        raise HTTPException(status_code=401, detail="tenant has not connected Stripe")

    try:
        verify_stripe_signature(
            body_bytes,
            stripe_signature,
            cfg.webhook_secret,
            now_s=int(time.time()),
        )
    except StripeSignatureError as exc:
        # Don't echo the body — just the failure reason. The signature header itself is fine to
        # mention but we drop it from log lines defensively.
        logger.warning("stripe webhook signature rejected (tenant=%s): %s", tenant, exc)
        raise HTTPException(status_code=400, detail=f"signature rejected: {exc}") from exc

    import json as _json

    try:
        event = _json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"body is not JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    mapped = map_stripe_event(event)
    if mapped is None:
        # Unsupported type — ack so Stripe doesn't retry. This is the right behavior even if the
        # tenant points a "send all events" subscription at us; we silently drop what we don't map.
        return JSONResponse(
            {"ok": True, "skipped": True, "reason": "unsupported event type"},
            status_code=200,
        )

    seen: set[tuple[str, str]] = app.state.stripe_event_seen
    key = (tenant, mapped.stripe_event_id)
    if key in seen:
        return JSONResponse(
            {"ok": True, "deduplicated": True, "event_id": mapped.stripe_event_id},
            status_code=200,
        )

    registry: HmacKeyRegistry = app.state.hmac_registry
    hashed = hash_customer_email(registry, tenant, mapped.customer_email)
    user_id_hash = hashed[0] if hashed else ""

    # CTO-195: the Stripe Customer is the account that owns this subscription/invoice, so it goes
    # to AccountIdHash. UserIdHash above is untouched. When Stripe named no customer (rare, but a
    # manual charge can arrive without one) the linker may still know which account this user
    # belongs to from an earlier event; if that user has ever been seen under two accounts it
    # answers with '' rather than picking one, and the conflict is logged as a DQ finding.
    stated = hash_stripe_customer(registry, tenant, mapped.stripe_customer_id)
    linker: AccountLinker = app.state.account_linker
    resolution = linker.resolve(
        tenant,
        user_id_hash=user_id_hash,
        stated_account_id_hash=stated[0] if stated else "",
        source="stripe",
    )
    if resolution.conflict is not None:
        logger.warning(
            "account identity conflict (tenant=%s): %s", tenant, resolution.conflict.as_dict()
        )

    # Build the BusinessEvent. ValueType is "monetary" for everything except churn (which is a
    # count event with value 0). Currency comes off the Stripe payload, defaulting to USD.
    value_type = "monetary"
    if mapped.event_name == "refund":
        value_type = "refund"
    elif mapped.event_name == "subscription_renewal":
        value_type = "mrr"
    elif mapped.event_name == "churn":
        value_type = "count"

    ev = BusinessEvent(
        business_event_id=mapped.stripe_event_id,
        event_name=mapped.event_name,
        user_id_hash=user_id_hash,
        occurred_at_ns=mapped.occurred_at_ns,
        value_amount_micro=mapped.value_amount_micro,
        value_currency=mapped.currency,
        value_type=value_type,
        source="stripe",
        account_id_hash=resolution.account_id_hash,
    )

    store: ClickHouseStore = app.state.store
    try:
        store.insert_business_events(tenant, [ev])
    except Exception:  # noqa: BLE001 — never crash the gateway on a CH blip
        logger.exception("clickhouse insert (stripe webhook) failed for tenant %s", tenant)
        # 503 → Stripe will retry, which is exactly what we want on a transient outage.
        raise HTTPException(status_code=503, detail="storage unavailable") from None

    seen.add(key)

    # CTO-117: stamp the integration run so the /connectors page lights up the Stripe card.
    # Best-effort — a postgres outage here must not turn a 200 into a 500 (Stripe would retry
    # and we'd double-count the business_event).
    integrations: TenantIntegrationStore = app.state.tenant_integrations
    try:
        integrations.record_run(tenant, "stripe", "success", event_count=1)
    except Exception:  # noqa: BLE001
        logger.exception("tenant_integration_runs upsert failed for tenant %s", tenant)

    return JSONResponse(
        {
            "ok": True,
            "event_id": mapped.stripe_event_id,
            "event_name": mapped.event_name,
            "value_amount_micro": mapped.value_amount_micro,
            "currency": mapped.currency,
            # CTO-195: whether this revenue reached an account, and whether that account was
            # stated by Stripe or inferred from the user. Never the hash itself: the response
            # goes back over the wire to Stripe and a hash is still an identifier.
            "account_attributed": resolution.is_attributed,
            "account_inferred": resolution.inferred,
        },
        status_code=200,
    )


# --------------------------------------------------------------------------------------------
# Unit economics — CAC inputs (CTO-111).
# --------------------------------------------------------------------------------------------


@app.get("/v1/tenant/cac")
def list_tenant_cac(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """List monthly CAC periods for the caller's tenant, newest first."""
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantCacStore = app.state.tenant_cac
    rows = store.list(tenant_id)
    return JSONResponse(
        {"tenant_id": tenant_id, "periods": [r.as_dict() for r in rows]},
        status_code=200,
    )


@app.post("/v1/tenant/cac")
async def upsert_tenant_cac(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Upsert one CAC period. Idempotent on (tenant_id, period_start).

    Rejects rows whose ``period_start`` is already closed (the successor month exists). Rejects
    rows whose ``new_customers_total < new_customers_paid`` (sanity guard).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    try:
        form = CacFormInput.from_json(body)
        period = app.state.tenant_cac.upsert(tenant_id, form)
    except CacPeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "period": period.as_dict()}, status_code=200
    )


@app.post("/v1/tenant/cac/csv")
async def upload_tenant_cac_csv(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Bulk-upsert CAC periods from a CSV body (fixed column order)."""
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    body = (await request.body()).decode("utf-8", errors="replace")
    try:
        forms = parse_csv(body)
        # Sort ascending so prior-period locking on each upsert holds.
        forms_sorted = sorted(forms, key=lambda f: f.period_start)
        periods = app.state.tenant_cac.upsert_many(tenant_id, forms_sorted)
    except CacPeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "imported": len(periods),
            "periods": [p.as_dict() for p in periods],
        },
        status_code=200,
    )


@app.get("/v1/tenant/cac/csv/template")
def download_tenant_cac_template(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
):
    """Return the CSV template (header + example row). Used by the upload UI."""
    from fastapi.responses import PlainTextResponse

    _ = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    return PlainTextResponse(
        csv_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cac_template.csv"'},
    )


@app.get("/v1/tenant/unit-economics/config")
def get_tenant_unit_economics_config(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """LTV/CAC band thresholds for the caller's tenant (CTO-126).

    Returns ``config: null`` when the tenant has no row — the web classify helpers then fall back to
    the hardcoded B2B-SaaS defaults. Same per-tenant auth as the CAC route.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantUnitEconomicsStore = app.state.tenant_unit_economics
    config = store.get(tenant_id)
    return JSONResponse(
        {"tenant_id": tenant_id, "config": config.as_dict() if config else None},
        status_code=200,
    )


@app.post("/v1/tenant/unit-economics/config")
async def upsert_tenant_unit_economics_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Upsert the tenant's LTV/CAC band thresholds. Idempotent on ``change_id`` (CTO-126).

    Body: ``{ltv_cac_green_threshold, ltv_cac_yellow_threshold, payback_months_green,
    payback_months_yellow, change_id, updated_by?}``. Replaying the same change_id is a no-op
    (returns the existing config unchanged). Rejects inverted bands (422).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    change_id = body.get("change_id")
    if not isinstance(change_id, str) or not change_id:
        raise HTTPException(status_code=422, detail="change_id required (uuid)")
    try:
        config_input = UnitEconomicsConfigInput.from_json(body)
        config = app.state.tenant_unit_economics.upsert(
            tenant_id,
            config_input,
            change_id=change_id,
            actor=body.get("updated_by"),
        )
    except UnitEconomicsConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "config": config.as_dict()}, status_code=200
    )


@app.get("/v1/tenant/revenue-sources/config")
def get_tenant_revenue_source_config(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Revenue source config for the caller's tenant (CTO-194).

    Returns ``config: null`` when the tenant has no row — the web reader then applies the defaults
    (every source counts; ValueType monetary + mrr are revenue; refunds net off). Same per-tenant
    auth as the unit-economics route.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: TenantRevenueSourceStore = app.state.tenant_revenue_sources
    try:
        config = store.get(tenant_id)
    except TenantNotFoundError as exc:
        # A caller identifying the tenant by NAME used to reach a UUID column and 500. Now it
        # resolves, and an unknown name is a plain 404 that says which name failed.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {"tenant_id": tenant_id, "config": config.as_dict() if config else None},
        status_code=200,
    )


@app.post("/v1/tenant/revenue-sources/config")
async def upsert_tenant_revenue_source_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Upsert the tenant's revenue source config. Idempotent on ``change_id`` (CTO-194).

    Body: ``{revenue_sources: string[] | null, include_mrr?: bool, change_id, updated_by?}``.
    ``revenue_sources: null`` means every source counts. An empty array is rejected (422) because
    "nothing is revenue" silently blanks the dashboard and is never what a caller means.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    change_id = body.get("change_id")
    if not isinstance(change_id, str) or not change_id:
        raise HTTPException(status_code=422, detail="change_id required (uuid)")
    try:
        config_input = RevenueSourceConfigInput.from_json(body)
        config = app.state.tenant_revenue_sources.upsert(
            tenant_id,
            config_input,
            change_id=change_id,
            actor=body.get("updated_by"),
        )
    except RevenueSourceConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "config": config.as_dict()}, status_code=200)


# --------------------------------------------------------------------------------------------
# CSV revenue upload (CTO-198) — for tenants whose revenue lives in a spreadsheet, not an API.
# --------------------------------------------------------------------------------------------

# Bound on the request body. A monthly finance export of even 50k accounts is well under a
# megabyte; this only stops a mistaken paste from becoming a memory incident.
MAX_REVENUE_CSV_BYTES = 8 * 1024 * 1024

REVENUE_CSV_TEMPLATE = (
    "account_id,period,amount,currency\n"
    "acct_1001,2026-08,12500.00,USD\n"
    "acct_1002,2026-08,4200.50,USD\n"
)


@app.get("/v1/tenant/revenue-uploads/template")
def get_revenue_upload_template() -> Response:
    """The exact CSV header the upload expects, with two example rows."""
    return Response(
        content=REVENUE_CSV_TEMPLATE,
        media_type="text/csv",
        headers={"content-disposition": 'attachment; filename="revenue-template.csv"'},
    )


@app.get("/v1/tenant/revenue-uploads")
def list_revenue_uploads(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Every uploaded revenue snapshot for the tenant, newest period first (CTO-198).

    The dashboard derives its "as of" date and staleness badge from ``uploaded_at``. An empty list
    means nothing has ever been uploaded, which the UI renders as an invitation rather than a zero.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    store: RevenueUploadStore = app.state.revenue_uploads
    try:
        rows = store.list(tenant_id)
    except RevenueUploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "source": UPLOAD_SOURCE,
            "snapshots": [r.as_dict() for r in rows],
        },
        status_code=200,
    )


def _revenue_policy_note(tenant_id: str) -> str | None:
    """Warn when the tenant's CTO-194 revenue-source narrowing would discard what they just sent.

    Silently dropped revenue is the exact bug CTO-194 fixed. A tenant who has narrowed
    ``revenue_sources`` to their biller and then uploads a spreadsheet would otherwise watch a
    successful upload change nothing on the dashboard, with no way to find out why.
    """
    try:
        config = app.state.tenant_revenue_sources.get(tenant_id)
    except Exception:  # noqa: BLE001 — an advisory note must never fail the upload
        logger.exception("revenue source config lookup failed for tenant %s", tenant_id)
        return None
    if config is None or not config.revenue_sources:
        return None
    if UPLOAD_SOURCE in config.revenue_sources:
        return None
    return (
        f"Uploaded, but this tenant's revenue sources are narrowed to "
        f"{', '.join(config.revenue_sources)}. Add '{UPLOAD_SOURCE}' on the revenue source config "
        f"or these rows will not be counted as revenue."
    )


@app.post("/v1/tenant/revenue-uploads")
async def upload_revenue_csv(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Upload ``account_id, period, amount, currency`` as business events (CTO-198).

    Body: ``{"csv": "...", "filename"?: str, "uploaded_by"?: str}``.

    All-or-nothing: a malformed row rejects the whole file with 422 and a per-line error list.
    Because an upload REPLACES the periods it covers, accepting the parseable half of a broken file
    would swap a complete snapshot for an incomplete one and silently delete the revenue of every
    account whose row failed.

    Idempotent by construction: each period's existing rows are deleted before the insert, and the
    ``BusinessEventId`` of every row is derived from ``(period, account hash)``. Re-uploading the
    same file leaves exactly the same rows behind.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    csv_text = body.get("csv")
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise HTTPException(status_code=422, detail="csv required (the file contents as a string)")
    if len(csv_text.encode("utf-8")) > MAX_REVENUE_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"csv is larger than {MAX_REVENUE_CSV_BYTES // (1024 * 1024)} MiB",
        )
    filename = body.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise HTTPException(status_code=422, detail="filename must be a string when provided")
    uploaded_by = body.get("uploaded_by")
    if uploaded_by is not None and not isinstance(uploaded_by, str):
        raise HTTPException(status_code=422, detail="uploaded_by must be a string when provided")

    try:
        parsed = parse_revenue_csv(csv_text)
    except RevenueUploadError as exc:
        # The per-line detail is the point of the ticket: a malformed row is rejected with a LINE
        # NUMBER, never silently skipped.
        return JSONResponse(exc.as_dict(), status_code=422)

    # Same per-tenant HMAC path the SDK and the Stripe connector use, so an uploaded account lands
    # in the same hash space as an instrumented one. The plaintext account id is used to compute
    # the digest and is never persisted or logged.
    registry: HmacKeyRegistry = app.state.hmac_registry
    registry.provision(tenant_id)
    snapshots = build_period_snapshots(
        parsed, hash_account=lambda account_id: registry.hash(tenant_id, account_id).value
    )

    store: ClickHouseStore = app.state.store
    uploads: RevenueUploadStore = app.state.revenue_uploads
    written: list[dict[str, Any]] = []
    for snapshot in snapshots:
        prefix = period_id_prefix(snapshot.period)
        try:
            store.delete_business_events_by_id_prefix(tenant_id, UPLOAD_SOURCE, prefix)
            store.insert_business_events(tenant_id, list(snapshot.events))
        except Exception:  # noqa: BLE001 — never crash the gateway on a CH blip
            logger.exception("clickhouse write (revenue upload) failed for tenant %s", tenant_id)
            raise HTTPException(status_code=503, detail="storage unavailable") from None
        try:
            row = uploads.record(
                tenant_id, snapshot, filename=filename, uploaded_by=uploaded_by
            )
        except RevenueUploadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception:  # noqa: BLE001
            # The events landed but the manifest did not, so nothing can say how fresh they are.
            # Revenue with no honest "as of" is worse than no revenue: roll the period back rather
            # than leave a number the dashboard would present as current forever.
            logger.exception("revenue upload manifest write failed for tenant %s", tenant_id)
            try:
                store.delete_business_events_by_id_prefix(tenant_id, UPLOAD_SOURCE, prefix)
            except Exception:  # noqa: BLE001
                logger.exception("rollback of revenue upload period %s failed", snapshot.period)
            raise HTTPException(status_code=503, detail="control plane unavailable") from None
        written.append(row.as_dict())

    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "source": UPLOAD_SOURCE,
            "accepted_rows": len(parsed.rows),
            "snapshots": written,
            "note": _revenue_policy_note(tenant_id),
        },
        status_code=200,
    )


@app.delete("/v1/tenant/revenue-uploads/{period}")
def delete_revenue_upload(
    period: str,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Remove one uploaded period, events and manifest row together (CTO-198).

    The escape hatch for a snapshot that turned out to be wrong. Deleting the events without the
    manifest row (or the other way round) would leave the dashboard claiming a freshness it cannot
    back, so both go in one call.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    if normalize_period(period) is None:
        raise HTTPException(status_code=422, detail="period must be a YYYY-MM calendar month")
    store: ClickHouseStore = app.state.store
    uploads: RevenueUploadStore = app.state.revenue_uploads
    try:
        store.delete_business_events_by_id_prefix(
            tenant_id, UPLOAD_SOURCE, period_id_prefix(period)
        )
    except Exception:  # noqa: BLE001
        logger.exception("clickhouse delete (revenue upload) failed for tenant %s", tenant_id)
        raise HTTPException(status_code=503, detail="storage unavailable") from None
    try:
        removed = uploads.delete(tenant_id, period)
    except RevenueUploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "period": period, "removed": removed})


# --------------------------------------------------------------------------------------------
# Generic revenue API (CTO-199).
# --------------------------------------------------------------------------------------------


@app.post("/v1/revenue/events")
async def ingest_revenue_event(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Post one revenue event for a biller we have no connector for (CTO-199).

    Body: ``{event_id, account_id, amount, currency, occurred_at, event_name}`` plus optional
    ``value_type`` (``monetary`` default, or ``mrr`` / ``refund`` / ``count``), ``user_id`` and
    ``properties``. Documented with a worked example in ``docs/revenue-api.md``.

    Idempotent on the caller-supplied ``event_id``, structurally rather than by convention. That id
    becomes ``business_events.BusinessEventId``, the table's own sort key, and a retry is refused
    twice over: an in-process deduplicator shared with the connector webhooks, and a ClickHouse
    probe on that key which is what still holds after a gateway restart. The endpoint will not mint
    an id for a caller who omits one, because an auto-generated id turns every retry into a second
    payment.

    Nothing here bypasses the revenue policy. The row is an ordinary ``business_events`` row, and
    the response reports whether the tenant's own configured revenue sources (CTO-194) will count
    it, so a narrowed policy shows up on the first request rather than as a blank dashboard later.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    ingestor: WebhookIngestor = app.state.revenue_ingestor
    try:
        result = ingestor.ingest_revenue_api(tenant_id, body)
    except RevenuePayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    event_id = str(body.get("event_id")).strip() if isinstance(body, dict) else ""
    if not result.accepted:
        # Seen in this process already: the cheap half of the idempotency guard.
        return JSONResponse(
            {"ok": True, "deduplicated": True, "event_id": event_id, "stored": False},
            status_code=200,
        )

    event = result.accepted[0]
    registry: HmacKeyRegistry = app.state.hmac_registry
    wire_event = to_wire_event(registry, tenant_id, event)
    store: ClickHouseStore = app.state.store

    try:
        already_stored = store.business_event_exists(tenant_id, wire_event.business_event_id)
    except Exception:  # noqa: BLE001
        # Un-mark before failing: leaving the id marked would make the caller's retry look like a
        # duplicate and lose the revenue outright, which is the same bug as double counting.
        ingestor.forget(tenant_id, wire_event.business_event_id)
        logger.exception("clickhouse idempotency probe failed for tenant %s", tenant_id)
        raise HTTPException(status_code=503, detail="storage unavailable") from None

    if already_stored:
        return JSONResponse(
            {"ok": True, "deduplicated": True, "event_id": event_id, "stored": True},
            status_code=200,
        )

    try:
        store.insert_business_events(tenant_id, [wire_event])
    except Exception:  # noqa: BLE001
        ingestor.forget(tenant_id, wire_event.business_event_id)
        logger.exception("clickhouse insert (revenue api) failed for tenant %s", tenant_id)
        raise HTTPException(status_code=503, detail="storage unavailable") from None

    counted = revenue_policy_note(
        app.state.tenant_revenue_sources, tenant_id, wire_event.source, wire_event.value_type
    )

    # Deliberately no tenant_integration_runs stamp (CTO-117). That table's connector column has a
    # CHECK constraint listing the five webhook integrations, so adding this source means a
    # migration; the /connectors card for it belongs with that migration, not wedged in here.

    return JSONResponse(
        {
            "ok": True,
            "deduplicated": False,
            "stored": True,
            "event_id": wire_event.business_event_id,
            "event_name": wire_event.event_name,
            "account_id_hash": wire_event.account_id_hash,
            "value_amount_micro": wire_event.value_amount_micro,
            "currency": wire_event.value_currency,
            "value_type": wire_event.value_type,
            "source": wire_event.source,
            # null, not false, when the tenant's revenue policy could not be read. An unknown
            # answer is reported as unknown.
            "counted_as_revenue": counted,
        },
        status_code=201,
    )


def _response_dict(resp: BatchResponse, *, replayed: bool = False) -> dict[str, Any]:
    hints = resp.server_hints or ServerHints()
    return {
        "batch_id": resp.batch_id,
        "status": resp.status.value,
        "accepted_spans": resp.accepted_spans,
        "partial_errors": [
            {"item_id": e.item_id, "code": e.code, "message": e.message} for e in resp.partial_errors
        ],
        "server_hints": {
            "flush_interval_ms": hints.flush_interval_ms,
            "max_batch_size": hints.max_batch_size,
            "sample_rate_override": hints.sample_rate_override,
            "retry_after_ms": hints.retry_after_ms,
        },
        "replayed": replayed,
    }


# --------------------------------------------------------------------------------------------
# Replay infrastructure (CTO-113) — sampling config, capture, projection.
# --------------------------------------------------------------------------------------------


@app.get("/v1/tenant/replay/config")
def get_tenant_replay_config(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Read the caller's replay-sampling config (CTO-113).

    Defaults to ``enabled=false`` when the tenant has no row yet — sampling is opt-in.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    cfg = app.state.tenant_replay.get(tenant_id)
    return JSONResponse({"tenant_id": tenant_id, "config": cfg.as_dict()})


@app.post("/v1/tenant/replay/config")
async def set_tenant_replay_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Toggle / tune replay sampling for the caller's tenant (CTO-113).

    Body fields (all optional — only what changes is updated):
    ``{enabled?: bool, sample_rate?: 0..1, retention_days?: int>0, daily_budget_usd?: number>=0}``.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    try:
        cfg = app.state.tenant_replay.upsert(
            tenant_id,
            enabled=body.get("enabled"),
            sample_rate=body.get("sample_rate"),
            retention_days=body.get("retention_days"),
            daily_budget_usd=body.get("daily_budget_usd"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "config": cfg.as_dict()})


def _as_int(value: object) -> int:
    """Coerce a span-attribute value to int, defaulting to 0. Token counts arrive as ints, but a
    malformed/absent value must never crash replay capture (CTO-237)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _extend_replay_index_bounded(
    sample_index: list,
    rows: list,
    *,
    per_tenant_cap: int | None = None,
) -> None:
    """Append ``rows`` to the in-memory index, then trim to the newest ``per_tenant_cap`` per tenant.

    CTO-237: the index is read on the hot ``/v1/replay`` + ``/v1/eval`` paths and lives for the
    life of the process, so it must not grow without bound. The list is append-ordered (oldest
    first), so newest-wins trimming keeps the tail. Kept tenant-scoped so a noisy tenant cannot
    evict another tenant's corpus. Mutates ``sample_index`` in place so app.state and every handler
    holding the same list object see the trim. ``per_tenant_cap`` defaults to the module constant,
    read at call time so it can be tuned/patched.
    """
    cap = per_tenant_cap if per_tenant_cap is not None else REPLAY_INDEX_PER_TENANT_CAP
    sample_index.extend(rows)
    counts: dict[str, int] = {}
    for r in sample_index:
        counts[r.tenant_id] = counts.get(r.tenant_id, 0) + 1
    over = {t for t, c in counts.items() if c > cap}
    if not over:
        return
    kept_reversed: list = []
    seen: dict[str, int] = {}
    for r in reversed(sample_index):
        if r.tenant_id in over:
            if seen.get(r.tenant_id, 0) >= cap:
                continue
            seen[r.tenant_id] = seen.get(r.tenant_id, 0) + 1
        kept_reversed.append(r)
    sample_index[:] = list(reversed(kept_reversed))


def capture_replay_samples_for_batch(
    tenant_id: str,
    candidates: list[SampleCandidate],
    *,
    config_store: TenantReplayStore | None = None,
    blob_store=None,
    sample_index: list | None = None,
    store: ClickHouseStore | None = None,
    captured_at=None,
) -> int:
    """Hook the gateway calls per accepted batch. Returns the number of samples persisted.

    Pulled out as a free function so tests can drive it without standing up a FastAPI app. The
    real ``POST /v1/batches`` path wires this in after the ingest pipeline writes to ClickHouse.

    Persistence (CTO-237): each sampled envelope is written to object storage (``persist_sample``)
    and its index row is (a) appended to the bounded in-memory ``sample_index`` that ``/v1/replay``
    and ``/v1/eval`` read, and (b) inserted into the ``replay_samples`` ClickHouse table when a
    ``store`` is provided, so the corpus survives a gateway restart and can be re-hydrated on boot.
    The ClickHouse write is best-effort: a failure there is logged but never loses the in-memory
    capture nor fails ingest.

    No-op (returns 0) when the tenant has not opted in.
    """
    import datetime as _dt
    config_store = config_store or app.state.tenant_replay
    blob_store = blob_store or app.state.replay_blob_store
    sample_index = sample_index if sample_index is not None else app.state.replay_sample_index
    captured_at = captured_at or _dt.datetime.now(_dt.timezone.utc)

    cfg = config_store.get(tenant_id)
    if not cfg.enabled or cfg.sample_rate <= 0 or not candidates:
        return 0

    sampled = stratified_sample(candidates, sample_rate=cfg.sample_rate)
    payloads = build_payloads(sampled, scrub=True)
    rows = [
        persist_sample(
            blob_store=blob_store,
            tenant_id=tenant_id,
            payload=p,
            captured_at=captured_at,
        )
        for p in payloads
    ]
    # Durable index (CTO-237): mirror the rows into ClickHouse so a restart can re-hydrate them.
    # Best-effort: the in-memory corpus is authoritative for this process either way.
    if rows and store is not None:
        try:
            store.insert_replay_samples(rows)
        except Exception:  # noqa: BLE001 - durable writeback is best-effort; keep the in-memory row
            logger.exception("replay: ClickHouse writeback failed for %d sample(s)", len(rows))
    _extend_replay_index_bounded(sample_index, rows)
    return len(rows)


@app.post("/v1/replay")
async def project_replay(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Project per-candidate cost/latency/error from replayed samples (CTO-113).

    Body::

        {
          "tenant_id": "...",           # optional — falls back to control-plane resolution
          "feature_tag": "research",    # optional filter
          "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4.5"}, ...],
          "sample_size": 50             # optional, default 50
        }

    Returns per candidate: ``projected_monthly_cost_micro_usd``, ``p50_latency_ms``,
    ``p95_latency_ms``, ``error_rate``, ``samples_replayed``, ``excluded_budget_count``.
    Plus ``samples_available`` (filter-matched corpus size before sampling) and a diagnostics
    block with the v1 honesty string ``"resolved-context replay (no live retrieval)"``.

    Synchronous: 60s timeout is fine for 50 samples × 3 candidates with the in-memory mock
    client; with a real provider client the executor's concurrency limit (5 per tenant) keeps
    things bounded.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    tenant_id = body.get("tenant_id") or _resolve_tenant_for_control_plane(
        authorization, x_tenant_id
    )
    feature_tag = body.get("feature_tag")
    candidates = body.get("candidate_models") or []
    if not isinstance(candidates, list) or not all(
        isinstance(c, dict) and "provider" in c and "model" in c for c in candidates
    ):
        raise HTTPException(
            status_code=422,
            detail="candidate_models must be a list of {provider, model} objects",
        )
    sample_size = int(body.get("sample_size") or 50)

    cfg = app.state.tenant_replay.get(tenant_id)
    index: list = app.state.replay_sample_index
    matching = [
        r for r in index
        if r.tenant_id == tenant_id
        and (feature_tag is None or r.feature_tag == feature_tag)
    ]
    samples_available = len(matching)

    if samples_available == 0:
        return JSONResponse({
            "tenant_id": tenant_id,
            "feature_tag": feature_tag,
            "samples_available": 0,
            "per_candidate": [],
            "diagnostics": {
                "context_fidelity": "resolved-context replay (no live retrieval)",
                "replay_cost_micro_usd": 0,
            },
        })

    # Pick samples stratified by token quintile (re-use the sampler's logic on the index).
    selected = _pick_for_projection(matching, sample_size)

    # Executor — uses a deterministic mock client by default so tests don't need a network.
    # Production deployments wire a real provider client here via app.state override.
    client = getattr(app.state, "replay_candidate_client", None) or _mock_candidate_client
    executor = ReplayExecutor(
        catalog=app.state.catalog,
        blob_store=app.state.replay_blob_store,
        client=client,
        todays_spend_micro_usd=lambda t: _todays_spend(app.state.replay_runs, t),
        sink=app.state.replay_runs.append,
    )

    per_candidate = []
    total_replay_cost = 0
    for cand in candidates:
        provider = str(cand["provider"])
        model = str(cand["model"])
        results = []
        excluded_budget = 0
        latencies: list[int] = []
        errors = 0
        cost_sum = 0
        for sample in selected:
            # Pre-flight cost estimate for the budget check: assume output = input tokens (50/50).
            from tally.pricing import Usage as _Usage
            from tally.pricing import compute_cost_micro_usd as _ccost
            est_cost, _ = _ccost(
                app.state.catalog, provider, model,
                _Usage(input_tokens=sample.input_tokens, output_tokens=sample.input_tokens),
            )
            result = await executor.replay_sample(
                tenant_id=tenant_id,
                sample_id=sample.sample_id,
                object_key=sample.s3_object_key,
                candidate_provider=provider,
                candidate_model=model,
                daily_budget_usd=cfg.daily_budget_usd,
                estimated_call_cost_micro_usd=est_cost,
            )
            results.append(result)
            if result.excluded_budget:
                excluded_budget += 1
                continue
            if result.row is not None:
                latencies.append(result.row.latency_ms)
                cost_sum += result.row.cost_micro_usd
                if result.row.error_msg:
                    errors += 1
        total_replay_cost += cost_sum
        # Project per-sample cost into a monthly figure by scaling to the *corpus* size.
        # `samples_available` is the matched corpus (after filter); we treat it as a
        # representative slice of the tenant's monthly volume on that feature_tag.
        replayed = len(results) - excluded_budget
        avg_cost = (cost_sum / replayed) if replayed > 0 else 0
        # Honest extrapolation: avg cost per call × corpus size, scaled by a 30/sample-window-days
        # factor of 30 — we don't track window days yet, so v1 just reports avg × corpus.
        projected_monthly_cost = int(round(avg_cost * samples_available))
        sorted_lat = sorted(latencies)
        p50 = sorted_lat[len(sorted_lat) // 2] if sorted_lat else 0
        p95 = sorted_lat[max(0, int(len(sorted_lat) * 0.95) - 1)] if sorted_lat else 0
        error_rate = (errors / replayed) if replayed > 0 else 0.0
        per_candidate.append({
            "provider": provider,
            "model": model,
            "projected_monthly_cost_micro_usd": projected_monthly_cost,
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "error_rate": error_rate,
            "samples_replayed": replayed,
            "excluded_budget_count": excluded_budget,
        })

    return JSONResponse({
        "tenant_id": tenant_id,
        "feature_tag": feature_tag,
        "samples_available": samples_available,
        "per_candidate": per_candidate,
        "diagnostics": {
            "context_fidelity": "resolved-context replay (no live retrieval)",
            "replay_cost_micro_usd": total_replay_cost,
        },
    })


@app.post("/v1/replay/estimate")
async def project_replay_estimate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Body-driven what-if: project one candidate with an optional prompt override (CTO-128).

    Body::

        {
          "tenant_id": "...",            # optional — falls back to control-plane resolution
          "feature_tag": "research",     # optional filter
          "candidate_model": {"provider": "anthropic", "model": "claude-haiku-4-5"},
          "system_prompt_override": "...",  # optional — applied to the envelope before pricing
          "sample_size": 50              # optional, default 50; sized to min(sample_size, available)
        }

    Reuses the ``/v1/replay`` executor + per-corpus extrapolation. The only new behavior is
    applying ``system_prompt_override`` to each captured envelope before the candidate call
    (see :func:`gateway.replay_estimate.apply_system_prompt_override`). Returns the same
    projection shape as ``/v1/replay`` (a single-element ``per_candidate`` list) plus the v1
    honesty/diagnostics block. Like ``/v1/replay``, the candidate client is injectable via
    ``app.state.replay_candidate_client`` (a deterministic mock by default).
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    tenant_id = body.get("tenant_id") or _resolve_tenant_for_control_plane(
        authorization, x_tenant_id
    )
    feature_tag = body.get("feature_tag")
    candidate = body.get("candidate_model")
    if not isinstance(candidate, dict) or "provider" not in candidate or "model" not in candidate:
        raise HTTPException(
            status_code=422,
            detail="candidate_model must be a {provider, model} object",
        )
    system_prompt_override = body.get("system_prompt_override")
    if system_prompt_override is not None and not isinstance(system_prompt_override, str):
        raise HTTPException(status_code=422, detail="system_prompt_override must be a string")
    sample_size = int(body.get("sample_size") or 50)

    cfg = app.state.tenant_replay.get(tenant_id)
    index: list = app.state.replay_sample_index
    matching = [
        r for r in index
        if r.tenant_id == tenant_id
        and (feature_tag is None or r.feature_tag == feature_tag)
    ]
    samples_available = len(matching)

    diagnostics = {
        "context_fidelity": "resolved-context replay (no live retrieval)",
        "prompt_override_applied": bool(system_prompt_override),
        # The override length is estimated at 4 chars/token — no real tokenizer in v1.
        "token_estimate": "4-chars-per-token (no live tokenizer)",
        "replay_cost_micro_usd": 0,
    }

    if samples_available == 0:
        return JSONResponse({
            "tenant_id": tenant_id,
            "feature_tag": feature_tag,
            "samples_available": 0,
            "per_candidate": [],
            "diagnostics": diagnostics,
        })

    # Size to min(sample_size, available); _pick_for_projection already clamps when size>=len.
    selected = _pick_for_projection(matching, min(sample_size, samples_available))

    client = getattr(app.state, "replay_candidate_client", None) or _mock_candidate_client
    executor = ReplayExecutor(
        catalog=app.state.catalog,
        blob_store=app.state.replay_blob_store,
        client=client,
        todays_spend_micro_usd=lambda t: _todays_spend(app.state.replay_runs, t),
        sink=app.state.replay_runs.append,
    )

    transform = (
        (lambda env: apply_system_prompt_override(env, system_prompt_override))
        if system_prompt_override
        else None
    )

    provider = str(candidate["provider"])
    model = str(candidate["model"])
    results = []
    excluded_budget = 0
    latencies: list[int] = []
    errors = 0
    cost_sum = 0
    for sample in selected:
        from tally.pricing import Usage as _Usage
        from tally.pricing import compute_cost_micro_usd as _ccost
        est_cost, _ = _ccost(
            app.state.catalog, provider, model,
            _Usage(input_tokens=sample.input_tokens, output_tokens=sample.input_tokens),
        )
        result = await executor.replay_sample(
            tenant_id=tenant_id,
            sample_id=sample.sample_id,
            object_key=sample.s3_object_key,
            candidate_provider=provider,
            candidate_model=model,
            daily_budget_usd=cfg.daily_budget_usd,
            estimated_call_cost_micro_usd=est_cost,
            envelope_transform=transform,
        )
        results.append(result)
        if result.excluded_budget:
            excluded_budget += 1
            continue
        if result.row is not None:
            latencies.append(result.row.latency_ms)
            cost_sum += result.row.cost_micro_usd
            if result.row.error_msg:
                errors += 1

    replayed = len(results) - excluded_budget
    avg_cost = (cost_sum / replayed) if replayed > 0 else 0
    projected_monthly_cost = int(round(avg_cost * samples_available))
    sorted_lat = sorted(latencies)
    p50 = sorted_lat[len(sorted_lat) // 2] if sorted_lat else 0
    p95 = sorted_lat[max(0, int(len(sorted_lat) * 0.95) - 1)] if sorted_lat else 0
    error_rate = (errors / replayed) if replayed > 0 else 0.0

    diagnostics["replay_cost_micro_usd"] = cost_sum

    return JSONResponse({
        "tenant_id": tenant_id,
        "feature_tag": feature_tag,
        "samples_available": samples_available,
        "per_candidate": [{
            "provider": provider,
            "model": model,
            "projected_monthly_cost_micro_usd": projected_monthly_cost,
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "error_rate": error_rate,
            "samples_replayed": replayed,
            "excluded_budget_count": excluded_budget,
        }],
        "diagnostics": diagnostics,
    })


def _pick_for_projection(rows: list, sample_size: int) -> list:
    """Token-quintile stratified pick from an index of ReplaySampleRow."""
    if sample_size >= len(rows):
        return rows
    # Approximate stratification: sort by token total, take every Nth.
    by_tokens = sorted(rows, key=lambda r: r.input_tokens + r.output_tokens)
    step = max(1, len(by_tokens) // sample_size)
    picked = by_tokens[::step][:sample_size]
    return picked


def _todays_spend(rows: list, tenant_id: str) -> int:
    """Sum today's replay_runs CostMicroUsd for `tenant_id`."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    return sum(
        r.cost_micro_usd for r in rows
        if r.tenant_id == tenant_id and r.ran_at.date() == today
    )


async def _mock_candidate_client(call: CandidateCall) -> CandidateResponse:
    """Deterministic mock — echoes back token counts from the envelope so executor tests are
    self-contained. Production deployments inject a real provider-routing client via
    ``app.state.replay_candidate_client``.

    The envelope is expected to carry ``{"input_tokens": int, "output_tokens": int}`` from the
    captured sample. Falls back to small defaults if missing.

    CTO-125: the mock also surfaces a ``response_text`` so the replay run persists candidate body
    text (real provider clients return the model's actual completion). We prefer an explicit
    ``candidate_response`` on the envelope, else the captured current ``response``.
    """
    env = call.envelope or {}
    response_text = ""
    for key in ("candidate_response", "response", "response_text", "completion"):
        v = env.get(key)
        if isinstance(v, str) and v:
            response_text = v
            break
    return CandidateResponse(
        input_tokens=int(env.get("input_tokens") or 100),
        output_tokens=int(env.get("output_tokens") or 50),
        response_text=response_text,
        finish_reason="stop",
        status_code=200,
    )


# --- Eval harness endpoints (CTO-114) ------------------------------------------------------------

@app.get("/v1/tenant/eval/config")
def get_tenant_eval_config(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Read the caller's pairwise-LLM-judge eval config (CTO-114).

    Defaults to ``enabled=false`` when the tenant has no row yet — eval is opt-in (judge calls
    burn real provider budget).
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    cfg = app.state.tenant_eval.get(tenant_id)
    return JSONResponse({"tenant_id": tenant_id, "config": cfg.as_dict()})


@app.post("/v1/tenant/eval/config")
async def set_tenant_eval_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Toggle / tune eval-harness for the caller's tenant (CTO-114).

    Body fields (all optional — only what changes is updated):
    ``{enabled?: bool, judge_model?: str, daily_budget_usd?: number>=0}``.
    """
    tenant_id = _resolve_tenant_for_control_plane(authorization, x_tenant_id)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    try:
        cfg = app.state.tenant_eval.upsert(
            tenant_id,
            enabled=body.get("enabled"),
            judge_model=body.get("judge_model"),
            daily_budget_usd=body.get("daily_budget_usd"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"tenant_id": tenant_id, "config": cfg.as_dict()})


@app.post("/v1/eval")
async def project_eval(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> JSONResponse:
    """Run a pairwise-LLM-judge pass over the replay corpus (CTO-114).

    Body::

        {
          "tenant_id": "...",           # optional — falls back to control-plane resolution
          "feature_tag": "research",    # optional filter
          "candidate_models": [{"provider": "anthropic", "model": "claude-haiku-4-5"}, ...],
          "sample_size": 50             # optional, default 50
        }

    For each candidate we find every replay_run with that (provider, model) for samples that
    belong to this tenant (optionally filtered by feature_tag), pair the candidate's response
    with the original captured response, and ask the judge which one better follows the
    instruction. The aggregate ``win_rate`` is ``candidate_wins / (candidate_wins + current_wins
    + ties)`` — errors are excluded from the denominator (they tell us the judge failed, not
    that the candidate did).

    Returns per-candidate::

        {
          provider, model,
          samples_judged, current_wins, candidate_wins, ties, errors,
          win_rate, win_rate_ci_lo, win_rate_ci_hi,
          judge_cost_micro_usd
        }

    Plus diagnostics (judge model, rubric version, excluded-budget count). Synchronous; v1
    fits inside a 10-minute timeout for typical 50-sample × 3-candidate passes against the
    in-memory mock judge.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    tenant_id = body.get("tenant_id") or _resolve_tenant_for_control_plane(
        authorization, x_tenant_id
    )
    feature_tag = body.get("feature_tag")
    candidates = body.get("candidate_models") or []
    if not isinstance(candidates, list) or not all(
        isinstance(c, dict) and "provider" in c and "model" in c for c in candidates
    ):
        raise HTTPException(
            status_code=422,
            detail="candidate_models must be a list of {provider, model} objects",
        )
    sample_size = int(body.get("sample_size") or 50)

    cfg = app.state.tenant_eval.get(tenant_id)
    sample_index: list = app.state.replay_sample_index
    replay_runs: list = app.state.replay_runs
    blob_store = app.state.replay_blob_store

    matching_samples = {
        r.sample_id: r for r in sample_index
        if r.tenant_id == tenant_id
        and (feature_tag is None or r.feature_tag == feature_tag)
    }
    samples_available = len(matching_samples)

    if samples_available == 0:
        return JSONResponse({
            "tenant_id": tenant_id,
            "feature_tag": feature_tag,
            "samples_available": 0,
            "per_candidate": [],
            "diagnostics": {
                "judge_model": cfg.judge_model,
                "rubric_version": "rubric-v1",
                "judge_cost_micro_usd": 0,
            },
        })

    # Pick the same stratified slice the replay projection would have picked. Reuse helper.
    selected_index_rows = _pick_for_projection(list(matching_samples.values()), sample_size)
    selected_ids = {r.sample_id for r in selected_index_rows}

    judge_client = getattr(app.state, "eval_judge_client", None) or _mock_judge_client
    executor = EvalExecutor(
        catalog=app.state.catalog,
        judge_client=judge_client,
        todays_spend_micro_usd=lambda t: _todays_eval_spend(app.state.eval_runs, t),
        sink=app.state.eval_runs.append,
        judge_provider="anthropic",
        judge_model=cfg.judge_model,
    )

    per_candidate = []
    total_judge_cost = 0
    for cand in candidates:
        provider = str(cand["provider"])
        model = str(cand["model"])
        # All replay_runs for this (tenant, candidate) on selected samples.
        cand_runs = [
            r for r in replay_runs
            if r.tenant_id == tenant_id
            and r.candidate_provider == provider
            and r.candidate_model == model
            and r.sample_id in selected_ids
            and not r.error_msg
        ]
        current_wins = 0
        candidate_wins = 0
        ties = 0
        errors = 0
        excluded_budget = 0
        cost_sum = 0
        for run in cand_runs:
            sample_row = matching_samples.get(run.sample_id)
            if sample_row is None:
                continue
            envelope = _load_envelope(blob_store, sample_row.s3_object_key)
            instruction = _extract_instruction(envelope)
            current_response = _extract_response(envelope)
            # Candidate response (CTO-125): the replay executor now persists the candidate model's
            # actual response body on the run row, so the judge grades what the candidate truly
            # produced rather than an envelope re-render. KEEP a fallback for legacy rows written
            # before this column existed (response_text absent → reconstruct from the envelope) so
            # historical eval results keep working.
            candidate_response = getattr(run, "response_text", "") or (
                envelope.get("candidate_response") or current_response
            )
            est_cost = _estimate_judge_cost(app.state.catalog, cfg.judge_model, instruction,
                                             current_response, candidate_response)
            result = await executor.judge_pair(
                tenant_id=tenant_id,
                replay_run_id=run.run_id,
                sample_id=run.sample_id,
                candidate_provider=provider,
                candidate_model=model,
                instruction=instruction,
                current_response=current_response,
                candidate_response=candidate_response,
                daily_budget_usd=cfg.daily_budget_usd,
                estimated_call_cost_micro_usd=est_cost,
            )
            if result.excluded_budget:
                excluded_budget += 1
                continue
            if result.row is not None:
                cost_sum += result.row.cost_micro_usd
            if result.verdict == "current_wins":
                current_wins += 1
            elif result.verdict == "candidate_wins":
                candidate_wins += 1
            elif result.verdict == "tie":
                ties += 1
            else:
                errors += 1
        total_judge_cost += cost_sum
        # Win-rate denominator excludes errors. Ties count toward the denominator (a tie is real
        # signal: "no clear winner") but not toward wins. Wilson CI computed in the web layer.
        non_error = current_wins + candidate_wins + ties
        win_rate = (candidate_wins / non_error) if non_error > 0 else 0.0
        # Wilson 95% interval for binomial proportion. Kept on the gateway side too so callers
        # without a Wilson helper get usable numbers — the web layer's wilsonInterval matches.
        lo, hi = _wilson_interval(candidate_wins, non_error)
        per_candidate.append({
            "provider": provider,
            "model": model,
            "samples_judged": non_error,
            "current_wins": current_wins,
            "candidate_wins": candidate_wins,
            "ties": ties,
            "errors": errors,
            "excluded_budget_count": excluded_budget,
            "win_rate": win_rate,
            "win_rate_ci_lo": lo,
            "win_rate_ci_hi": hi,
            "judge_cost_micro_usd": cost_sum,
        })

    return JSONResponse({
        "tenant_id": tenant_id,
        "feature_tag": feature_tag,
        "samples_available": samples_available,
        "per_candidate": per_candidate,
        "diagnostics": {
            "judge_model": cfg.judge_model,
            "rubric_version": "rubric-v1",
            "judge_cost_micro_usd": total_judge_cost,
        },
    })


def _load_envelope(blob_store, object_key: str) -> dict:
    try:
        import json as _json
        return _json.loads(blob_store.get_bytes(object_key).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _extract_instruction(envelope: dict) -> str:
    """Pull the user instruction out of an envelope. Resilient to several shapes."""
    if not envelope:
        return ""
    for key in ("prompt", "instruction", "user_message"):
        v = envelope.get(key)
        if isinstance(v, str) and v:
            return v
    msgs = envelope.get("messages")
    if isinstance(msgs, list):
        # Last user-role message wins.
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str):
                    return content
    return ""


def _extract_response(envelope: dict) -> str:
    """Pull the captured current-model response text out of an envelope."""
    if not envelope:
        return ""
    for key in ("response", "response_text", "completion"):
        v = envelope.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _estimate_judge_cost(catalog, model: str, *texts: str) -> int:
    """Rough pre-flight cost estimate — 4 chars/token, fixed 50-token output budget for the
    short A/B/TIE answer. Used only for the budget check; the row's actual CostMicroUsd uses
    real token counts from the judge response.
    """
    from tally.pricing import Usage as _Usage
    from tally.pricing import compute_cost_micro_usd as _ccost
    char_total = sum(len(t) for t in texts if isinstance(t, str))
    input_tokens = max(50, char_total // 4)
    cost, _ = _ccost(catalog, "anthropic", model, _Usage(input_tokens=input_tokens, output_tokens=50))
    return cost


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 0.0
    p = successes / trials
    denom = 1 + (z * z) / trials
    center = (p + (z * z) / (2 * trials)) / denom
    import math as _math
    half = (z * _math.sqrt((p * (1 - p)) / trials + (z * z) / (4 * trials * trials))) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _todays_eval_spend(rows: list, tenant_id: str) -> int:
    """Sum today's eval_runs CostMicroUsd for `tenant_id`."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    return sum(
        r.cost_micro_usd for r in rows
        if r.tenant_id == tenant_id and r.judged_at.date() == today
    )


async def _mock_judge_client(call: JudgeCall) -> JudgeResponse:
    """Deterministic MOCK judge for local dev + tests (CTO-237). NOT a real judge: production
    wires a real Anthropic client via ``app.state.eval_judge_client``.

    The previous mock always emitted "TIE", which made every candidate's win-rate 0.0 and left the
    replay-backed Compare/eval and the wrong-sized-model waste detector unable to produce the one
    signal they exist for: "this cheaper model is statistically indistinguishable in quality".

    This mock instead derives a verdict letter deterministically from the rubric prompt (same
    prompt -> same letter, so tests stay stable) with a balanced split: ~47.5% A, ~47.5% B, ~5%
    TIE. The executor already randomizes A/B placement per sample (position-bias mitigation), so a
    balanced A/B letter maps to a candidate win-rate of ~(1 - tie_fraction)/2 ≈ 0.475 whose Wilson
    CI overlaps 0.5 at the sample sizes real Compare/eval passes use. That "statistically
    indistinguishable" outcome is exactly the honest right-sizing signal the wrong-sized-model
    waste detector keys on; the mock deliberately does NOT favor the candidate. The small tie
    fraction keeps win-rate near 0.5 (ties count in the win-rate denominator, so a large tie share
    would drag it down and let a fair candidate look worse than it is).
    """
    # blake2b over the full prompt (instruction + both responses) -> a stable, uniform bucket.
    import hashlib
    bucket = int.from_bytes(
        hashlib.blake2b(call.prompt.encode("utf-8"), digest_size=8).digest(), "big"
    ) % 40
    if bucket < 19:
        text = "A"
    elif bucket < 38:
        text = "B"
    else:
        text = "TIE"
    input_tokens = max(10, len(call.prompt) // 4)
    return JudgeResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=2,
        status_code=200,
    )
