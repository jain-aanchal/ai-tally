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
from tally.timekeeping import assess
from tally.wire import (
    BatchRequest,
    BatchResponse,
    BusinessEvent,
    IdempotencyCache,
    IdentityLink,
    Sampling,
    Status,
    uuid7,
)

from gateway.auth import ApiKeyAuth
from gateway.config import get_settings
from gateway.errors import ErrorCode
from gateway.mapping import span_to_row
from gateway.ratelimit import RateLimiter
from gateway.store import ClickHouseStore

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
    logger.info("gateway up (require_api_key=%s)", settings.require_api_key)
    yield
    app.state.store.close()


app = FastAPI(title="ai-tally ingest gateway", version="0.1.0", lifespan=lifespan)


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


@app.post("/v1/batches")
async def ingest_batch(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings = app.state.settings
    store: ClickHouseStore = app.state.store
    auth: ApiKeyAuth = app.state.auth
    catalog = app.state.catalog
    idempotency: IdempotencyCache = app.state.idempotency
    limiter: RateLimiter = app.state.limiter

    payload = await request.json()
    batch = _parse_batch(payload)
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

    # --- enrich + map spans ---
    rows: list[tuple[object, ...]] = []
    drift_count = 0
    for span in batch.resource_spans:
        if not isinstance(span, dict):
            continue
        result = enrich_cost(span, catalog, tenant_id=batch.tenant_id)
        if result.drift_exceeded:
            drift_count += 1
        client_ts = span.get("timestamp_ns")
        client_ts_ns = client_ts if isinstance(client_ts, int) else batch.client_send_ts_ns
        skew = assess(client_ts_ns, server_recv_ns)
        rows.append(
            span_to_row(
                result.attributes,
                tenant_id=batch.tenant_id,
                effective_ts_ns=skew.effective_ts_ns,
                sample_rate=batch.sampling.head_sample_rate,
            )
        )

    # --- write ---
    try:
        accepted = store.insert_spans(rows)
        store.insert_business_events(batch.tenant_id, batch.business_events)
        store.insert_identity_links(batch.tenant_id, batch.identity_links)
    except Exception:  # noqa: BLE001 - surface as retryable, keep the gateway alive
        logger.exception("clickhouse insert failed")
        resp = BatchResponse(batch_id=batch.batch_id, status=Status.RETRY)
        idempotency.record(batch, resp)
        return JSONResponse(_response_dict(resp), status_code=503)

    if drift_count:
        logger.info("catalog drift on %d/%d spans (batch %s)", drift_count, len(rows), batch.batch_id)

    resp = BatchResponse(batch_id=batch.batch_id, status=Status.ACCEPTED, accepted_spans=accepted)
    idempotency.record(batch, resp)
    return JSONResponse(_response_dict(resp), status_code=200)


def _response_dict(resp: BatchResponse, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "batch_id": resp.batch_id,
        "status": resp.status.value,
        "accepted_spans": resp.accepted_spans,
        "replayed": replayed,
    }
