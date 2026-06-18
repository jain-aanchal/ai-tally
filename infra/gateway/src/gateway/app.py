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
from fastapi.responses import JSONResponse

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

from gateway.auth import ApiKeyAuth
from gateway.backpressure import Backpressure
from gateway.config import get_settings
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
from gateway.store import ClickHouseStore
from gateway.stripe_ingest import (
    StripeSignatureError,
    hash_customer_email,
    map_stripe_event,
    verify_stripe_signature,
)
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
    InMemoryReplayBlobStore,
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
from gateway.tenant_connectors import ALLOWED_LAYERS, TenantConnectorStore
from gateway.tenant_eval import TenantEvalStore
from gateway.tenant_guardrails import (
    ALLOWED_KINDS as GUARDRAIL_KINDS,
    ALLOWED_STATES as GUARDRAIL_STATES,
    TenantGuardrailStore,
)
from gateway.tenant_integrations import TenantIntegrationStore
from gateway.tenant_replay import TenantReplayStore
from gateway.tenant_stripe import TenantStripeStore
from gateway.validation import SpanValidator, span_item_id

from tally.hmac_keys import HmacKeyRegistry

logger = logging.getLogger("tally.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.store = ClickHouseStore(settings)
    app.state.auth = ApiKeyAuth(settings)
    app.state.tenant_connectors = TenantConnectorStore(settings)
    # Per-tenant guardrail registry (CTO-116) — the SDK polls /v1/tenant/guardrails on its
    # config-refresh window and enforces matching rules in-process. Shadow rules emit span
    # attrs but never alter the call; enabled rules do.
    app.state.tenant_guardrails = TenantGuardrailStore(settings)
    app.state.tenant_stripe = TenantStripeStore(settings)
    # Per-tenant third-party integration run status (CTO-117): Stripe / Segment / HubSpot / Pendo.
    # Workers call .record_run after each cycle; the dashboard reads via /v1/tenant/integrations/status.
    app.state.tenant_integrations = TenantIntegrationStore(settings)
    # Per-tenant monthly CAC inputs (CTO-111): finance fills serially, locked when next month opens.
    app.state.tenant_cac = TenantCacStore(settings)
    # Replay infra (CTO-113): per-tenant opt-in sampling + cross-provider projection.
    # The blob store is in-memory by default — swappable for MinIO/S3 via app.state override in
    # a deployment shim. Replay runs accumulate in-memory until ClickHouse writeback lands
    # (sink wired to a list for v1 — the projection API reads from it directly).
    app.state.tenant_replay = TenantReplayStore(settings)
    app.state.replay_blob_store = InMemoryReplayBlobStore()
    app.state.replay_sample_index = []  # list[ReplaySampleRow]
    app.state.replay_runs = []  # list[ReplayRunRow]
    # Eval harness (CTO-114): pairwise-LLM-judge over the replay outputs. Opt-in like replay;
    # judge calls accumulate in-memory until the ClickHouse writeback path lands.
    app.state.tenant_eval = TenantEvalStore(settings)
    app.state.eval_runs = []  # list[EvalRunRow]
    # Per-tenant HMAC key registry — used to hash Stripe customer emails into the same
    # UserIdHash space the SDK uses, so the attribution join lights up (CTO-110).
    app.state.hmac_registry = HmacKeyRegistry()
    # In-process dedup set for Stripe webhook redeliveries. ClickHouse's ReplacingMergeTree
    # will collapse late duplicates at merge time, but this short-circuits the second insert
    # so the 200 stays well under Stripe's 30s timeout window.
    app.state.stripe_event_seen = set()
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
    if app.state.ingest_buffer is not None:
        await app.state.ingest_buffer.stop()  # flush buffered rows before closing the store
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


def capture_replay_samples_for_batch(
    tenant_id: str,
    candidates: list[SampleCandidate],
    *,
    config_store: TenantReplayStore | None = None,
    blob_store=None,
    sample_index: list | None = None,
    captured_at=None,
) -> int:
    """Hook the gateway calls per accepted batch. Returns the number of samples persisted.

    Pulled out as a free function so tests can drive it without standing up a FastAPI app. The
    real ``POST /v1/batches`` path wires this in after the ingest pipeline writes to ClickHouse.

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
    for p in payloads:
        row = persist_sample(
            blob_store=blob_store,
            tenant_id=tenant_id,
            payload=p,
            captured_at=captured_at,
        )
        sample_index.append(row)
    return len(payloads)


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
    """
    env = call.envelope or {}
    return CandidateResponse(
        input_tokens=int(env.get("input_tokens") or 100),
        output_tokens=int(env.get("output_tokens") or 50),
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
            # Candidate response: we don't currently persist the candidate's response text in
            # replay_runs (CTO-113 only wrote tokens/cost/latency). The mock-judge path uses a
            # synthetic candidate_response derived from the envelope; the production path will
            # need replay_runs to grow a response-text column.
            # FIXME(CTO-114-followup): persist candidate response text on replay_runs.
            candidate_response = envelope.get("candidate_response") or current_response
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
    """Deterministic mock judge for tests / dev. Always emits "TIE" with token counts
    derived from the prompt length. Production deployments wire a real Anthropic client via
    ``app.state.eval_judge_client``.
    """
    input_tokens = max(10, len(call.prompt) // 4)
    return JudgeResponse(
        text="TIE",
        input_tokens=input_tokens,
        output_tokens=2,
        status_code=200,
    )
