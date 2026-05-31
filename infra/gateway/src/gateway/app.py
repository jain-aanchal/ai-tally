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
from gateway.validation import SpanValidator, span_item_id

logger = logging.getLogger("tally.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.store = ClickHouseStore(settings)
    app.state.auth = ApiKeyAuth(settings)
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
    logger.info("gateway up (require_api_key=%s)", settings.require_api_key)
    yield
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
