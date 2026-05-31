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
    Sampling,
    Status,
    uuid7,
)

from gateway.auth import ApiKeyAuth
from gateway.config import get_settings
from gateway.mapping import span_to_row
from gateway.metering import UsageRollup
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
    app.state.metering = UsageRollup()
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
    metering: UsageRollup = app.state.metering

    payload = await request.json()
    batch = _parse_batch(payload)

    # --- auth: resolve tenant ---
    if settings.require_api_key:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        tenant_id = auth.tenant_for_key(token)
        if tenant_id is None:
            raise HTTPException(status_code=403, detail="invalid api key")
        batch.tenant_id = tenant_id  # trust the key's tenant, not the body
    elif not batch.tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id required when auth is disabled")

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
    return {
        "batch_id": resp.batch_id,
        "status": resp.status.value,
        "accepted_spans": resp.accepted_spans,
        "replayed": replayed,
    }
