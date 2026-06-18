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
from gateway.tenant_connectors import ALLOWED_LAYERS, TenantConnectorStore
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
    app.state.tenant_stripe = TenantStripeStore(settings)
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
