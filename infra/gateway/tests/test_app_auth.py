"""Gateway auth + rate-limit wiring tests (CTO-33).

These exercise the rejection paths only — every assertion here returns *before* any ClickHouse
write, so the tests need no running infra. The happy path (which inserts) is covered by the pure
limiter/mapping tests plus the live smoke in the PR description.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from gateway.app import app
from gateway.auth import AuthResult
from gateway.mapping import COLUMNS
from gateway.ratelimit import RateLimiter


class FakeAuth:
    """In-memory token → AuthResult map standing in for the Postgres control plane."""

    def __init__(self, mapping: dict[str, AuthResult]) -> None:
        self._mapping = mapping

    def authenticate(self, token: str) -> AuthResult | None:
        return self._mapping.get(token)

    def ping(self) -> bool:
        return True


TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


def _batch(tenant_id: str, n_spans: int = 1) -> dict:
    return {
        "tenant_id": tenant_id,
        "sdk_version": "test",
        "resource_spans": [{"trace_id": f"t{i}", "span_id": f"s{i}"} for i in range(n_spans)],
    }


class RecordingStore:
    """Captures the row tuples the ingest path would insert, so no ClickHouse is needed."""

    def __init__(self) -> None:
        self.spans: list[tuple] = []

    def insert_spans(self, rows: list[tuple]) -> int:
        self.spans.extend(rows)
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@contextmanager
def _client(
    *,
    keys: dict[str, AuthResult] | None = None,
    limiter: RateLimiter | None = None,
    store: RecordingStore | None = None,
) -> Iterator[TestClient]:
    # `with TestClient(app)` runs the lifespan exactly once; we then override the infra-touching
    # bits (auth → in-memory, optionally the limiter or the store) before yielding.
    with TestClient(app) as client:
        # `app` is module-level and shared by every test in the run, so each override is put back on
        # the way out. Leaving a RecordingStore (or a FakeAuth) parked on app.state leaks into
        # whatever runs next and makes failures depend on test order.
        previous = {
            "require_api_key": app.state.settings.require_api_key,
            "auth": app.state.auth,
            "limiter": app.state.limiter,
            "store": app.state.store,
        }
        try:
            app.state.settings.require_api_key = True
            app.state.auth = FakeAuth(keys or {})
            if limiter is not None:
                app.state.limiter = limiter
            if store is not None:
                app.state.store = store
            yield client
        finally:
            app.state.settings.require_api_key = previous["require_api_key"]
            app.state.auth = previous["auth"]
            app.state.limiter = previous["limiter"]
            app.state.store = previous["store"]


def test_missing_bearer_is_unauthenticated() -> None:
    with _client(keys={}) as c:
        r = c.post("/v1/batches", json=_batch(TENANT_A))
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "UNAUTHENTICATED"


def test_invalid_key_is_unauthenticated() -> None:
    with _client(keys={}) as c:
        r = c.post("/v1/batches", json=_batch(TENANT_A), headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "UNAUTHENTICATED"


def test_read_scope_cannot_write() -> None:
    keys = {"rk": AuthResult(tenant_id=TENANT_A, scope="read")}
    with _client(keys=keys) as c:
        r = c.post("/v1/batches", json=_batch(TENANT_A), headers={"Authorization": "Bearer rk"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "FORBIDDEN_SCOPE"


def test_tenant_a_key_cannot_write_tenant_b() -> None:
    keys = {"wk": AuthResult(tenant_id=TENANT_A, scope="write")}
    with _client(keys=keys) as c:
        r = c.post("/v1/batches", json=_batch(TENANT_B), headers={"Authorization": "Bearer wk"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "TENANT_MISMATCH"


def test_ingest_stamps_the_tenant_uuid_on_every_span() -> None:
    """Canonical TenantId is the tenant UUID (Initiative 1, §8, Decision 4).

    ``db/clickhouse/otel_spans.sql`` orders by ``TenantId`` first and the dashboard binds the UUID
    into its read filter, so a span stamped with anything else (a tenant NAME, say) is invisible to
    every read. The key's tenant is authoritative and it is a UUID, so this pins the written value
    rather than trusting the ingest path to keep agreeing with the reader by coincidence.
    """
    keys = {"wk": AuthResult(tenant_id=TENANT_A, scope="write")}
    store = RecordingStore()
    with _client(keys=keys, store=store) as c:
        r = c.post(
            "/v1/batches",
            json=_batch(TENANT_A, n_spans=2),
            headers={"Authorization": "Bearer wk"},
        )
        assert r.status_code == 200

    assert len(store.spans) == 2
    tenant_col = COLUMNS.index("TenantId")
    written = {row[tenant_col] for row in store.spans}
    assert written == {TENANT_A}
    # Guard against a future refactor that stamps a name: parseable as a UUID, not a label.
    uuid.UUID(written.pop())


def test_rate_limit_exceeded_returns_429_with_retry_after() -> None:
    keys = {"wk": AuthResult(tenant_id=TENANT_A, scope="write")}
    # burst=2 but the batch carries 5 spans → rate-limited before any insert.
    limiter = RateLimiter(rps=1, burst=2, monthly_quota=1_000_000)
    with _client(keys=keys, limiter=limiter) as c:
        r = c.post(
            "/v1/batches",
            json=_batch(TENANT_A, n_spans=5),
            headers={"Authorization": "Bearer wk"},
        )
        assert r.status_code == 429
        body = r.json()
        assert body["status"] == "rejected"
        assert body["error"]["code"] == "RATE_LIMITED"
        assert int(r.headers["Retry-After"]) >= 1
        assert body["retry_after_ms"] > 0
