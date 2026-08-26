"""App-level tests for POST /v1/revenue/events (CTO-199).

Verification path: fake the ClickHouse store so we can assert exactly what got inserted and can
make the durable idempotency probe answer the way a restarted gateway would, and fake the revenue
source config store so the policy answer on the response is exercised without Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from gateway.app import app
from gateway.revenue_api import counts_as_revenue
from gateway.tenant_revenue_sources import RevenueSourceConfig
from tally.cdp_connectors import WebhookIngestor
from tally.wire import BusinessEvent

TENANT = "t-acme"


def _payload(**overrides) -> dict:
    body = {
        "event_id": "inv_2026_08_0001",
        "account_id": "acct_northwind",
        "amount": "1499.00",
        "currency": "USD",
        "occurred_at": "2026-08-25T12:00:00Z",
        "event_name": "invoice_paid",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not _OMIT}


_OMIT = object()


class FakeCHStore:
    """Records inserts; ``existing`` seeds ids a previous gateway process already wrote.

    Keyed on ``(tenant, id)`` because the real probe is tenant-scoped: two tenants may legitimately
    use the same invoice number.
    """

    def __init__(self, existing: set[str] | None = None, tenant: str = TENANT) -> None:
        self.events: list[BusinessEvent] = []
        self.existing: set[tuple[str, str]] = {(tenant, e) for e in (existing or ())}
        self.probe_calls = 0
        self.fail_probe = False
        self.fail_insert = False

    def business_event_exists(self, tenant_id: str, business_event_id: str) -> bool:
        self.probe_calls += 1
        if self.fail_probe:
            raise RuntimeError("clickhouse down")
        return (tenant_id, business_event_id) in self.existing

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        if self.fail_insert:
            raise RuntimeError("clickhouse down")
        self.events.extend(events)
        self.existing.update((tenant_id, e.business_event_id) for e in events)
        return len(events)

    def insert_spans(self, rows: list[tuple]) -> int:
        return 0

    def insert_identity_links(self, tenant_id: str, links: list) -> int:
        return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeRevenueSourceStore:
    def __init__(self, config: RevenueSourceConfig | None = None, raises: bool = False) -> None:
        self._config = config
        self._raises = raises

    def get_for_caller(self, tenant_id: str) -> RevenueSourceConfig | None:
        if self._raises:
            raise RuntimeError("postgres down")
        return self._config


def _config(sources, include_mrr: bool = True) -> RevenueSourceConfig:
    return RevenueSourceConfig(
        revenue_sources=sources,
        include_mrr=include_mrr,
        created_at=None,
        updated_at=None,
        updated_by=None,
    )


@contextmanager
def _client(
    store: FakeCHStore | None = None,
    revenue_sources: FakeRevenueSourceStore | None = None,
) -> Iterator[tuple[TestClient, FakeCHStore]]:
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        ch = store or FakeCHStore()
        app.state.store = ch
        app.state.tenant_revenue_sources = revenue_sources or FakeRevenueSourceStore()
        # Fresh deduplicator per test: the in-process guard is process-lifetime state.
        app.state.revenue_ingestor = WebhookIngestor()
        yield client, ch


def _post(client: TestClient, body: dict, tenant: str = TENANT):
    return client.post("/v1/revenue/events", json=body, headers={"X-Tenant-Id": tenant})


# ----------------------------------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------------------------------
def test_accepts_a_revenue_event_and_writes_one_business_event():
    with _client() as (client, ch):
        res = _post(client, _payload())
        assert res.status_code == 201
        body = res.json()
        assert body["deduplicated"] is False
        assert body["value_amount_micro"] == 1_499_000_000
        assert body["value_type"] == "monetary"
        assert body["source"] == "revenue-api"
        assert body["counted_as_revenue"] is True

        assert len(ch.events) == 1
        ev = ch.events[0]
        assert ev.business_event_id == "inv_2026_08_0001"
        assert ev.value_currency == "USD"
        # The account id is hashed, never stored raw, and lands in the account dimension.
        assert len(ev.account_id_hash) == 64
        assert "acct_northwind" not in ev.account_id_hash
        assert body["account_id_hash"] == ev.account_id_hash


def test_account_id_doubles_as_the_user_identity_when_no_user_id_is_given():
    # Parity with the Stripe connector, which hashes the customer id into UserIdHash. It is what
    # makes this revenue join in the attribution query like connector-sourced revenue.
    with _client() as (client, ch):
        _post(client, _payload())
        ev = ch.events[0]
        assert ev.user_id_hash == ev.account_id_hash


def test_an_explicit_user_id_is_hashed_separately_from_the_account():
    with _client() as (client, ch):
        _post(client, _payload(user_id="u_dana"))
        ev = ch.events[0]
        assert len(ev.user_id_hash) == 64
        assert ev.user_id_hash != ev.account_id_hash


def test_count_event_stores_a_null_amount_never_zero():
    with _client() as (client, ch):
        res = _post(
            client,
            _payload(value_type="count", amount=_OMIT, event_name="seat_activated"),
        )
        assert res.status_code == 201
        assert ch.events[0].value_amount_micro is None
        assert res.json()["counted_as_revenue"] is False


def test_currency_is_recorded_not_converted():
    with _client() as (client, ch):
        res = _post(client, _payload(currency="eur", amount="100.00"))
        assert res.status_code == 201
        assert ch.events[0].value_currency == "EUR"
        assert ch.events[0].value_amount_micro == 100_000_000


# ----------------------------------------------------------------------------------------------
# Idempotency: the requirement most likely to be got wrong
# ----------------------------------------------------------------------------------------------
def test_retry_with_the_same_event_id_does_not_double_count():
    with _client() as (client, ch):
        first = _post(client, _payload())
        second = _post(client, _payload())
        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["deduplicated"] is True
        assert len(ch.events) == 1


def test_retry_after_a_restart_is_still_deduped_by_the_clickhouse_probe():
    # A fresh process has an empty in-process set, so the durable probe is the only thing left.
    ch = FakeCHStore(existing={"inv_2026_08_0001"})
    with _client(store=ch) as (client, store):
        res = _post(client, _payload())
        assert res.status_code == 200
        assert res.json() == {
            "ok": True,
            "deduplicated": True,
            "event_id": "inv_2026_08_0001",
            "stored": True,
        }
        assert store.events == []


def test_a_changed_amount_under_a_reused_event_id_does_not_add_revenue():
    with _client() as (client, ch):
        _post(client, _payload())
        res = _post(client, _payload(amount="99999.00"))
        assert res.status_code == 200
        assert len(ch.events) == 1
        assert ch.events[0].value_amount_micro == 1_499_000_000


def test_idempotency_is_tenant_scoped():
    with _client() as (client, ch):
        assert _post(client, _payload(), tenant="t-one").status_code == 201
        assert _post(client, _payload(), tenant="t-two").status_code == 201
        assert len(ch.events) == 2


def test_a_failed_write_leaves_the_event_id_retryable():
    # The mirror-image bug of double counting: swallowing the retry and losing the revenue.
    ch = FakeCHStore()
    ch.fail_insert = True
    with _client(store=ch) as (client, store):
        assert _post(client, _payload()).status_code == 503
        store.fail_insert = False
        assert _post(client, _payload()).status_code == 201
        assert len(store.events) == 1


def test_a_failed_idempotency_probe_refuses_rather_than_risking_a_double_write():
    ch = FakeCHStore()
    ch.fail_probe = True
    with _client(store=ch) as (client, store):
        assert _post(client, _payload()).status_code == 503
        assert store.events == []
        store.fail_probe = False
        assert _post(client, _payload()).status_code == 201


# ----------------------------------------------------------------------------------------------
# Validation: a documented endpoint has to say what is wrong
# ----------------------------------------------------------------------------------------------
def test_a_missing_event_id_is_rejected_rather_than_auto_generated():
    with _client() as (client, ch):
        res = _post(client, _payload(event_id=_OMIT))
        assert res.status_code == 422
        assert "event_id" in res.json()["detail"]
        assert ch.events == []


def test_bad_fields_name_the_field_at_fault():
    with _client() as (client, _ch):
        assert "occurred_at" in _post(client, _payload(occurred_at="soon")).json()["detail"]
        assert "amount" in _post(client, _payload(amount="lots")).json()["detail"]
        assert "currency" in _post(client, _payload(currency="dollars")).json()["detail"]


def test_a_negative_amount_is_rejected_with_the_refund_route_named():
    with _client() as (client, _ch):
        res = _post(client, _payload(amount="-10.00"))
        assert res.status_code == 422
        assert "refund" in res.json()["detail"]


def test_tenant_header_is_required_when_auth_is_off():
    with TestClient(app) as client:
        app.state.settings.require_api_key = False
        app.state.store = FakeCHStore()
        app.state.revenue_ingestor = WebhookIngestor()
        assert client.post("/v1/revenue/events", json=_payload()).status_code == 422


# ----------------------------------------------------------------------------------------------
# Revenue policy (CTO-194): this endpoint narrows under it, it does not bypass it
# ----------------------------------------------------------------------------------------------
def test_a_narrowed_policy_that_excludes_this_source_says_so_on_the_response():
    store = FakeRevenueSourceStore(_config(("stripe",)))
    with _client(revenue_sources=store) as (client, ch):
        res = _post(client, _payload())
        assert res.status_code == 201
        # Still stored: the tenant's config decides what is *counted*, not what is accepted.
        assert len(ch.events) == 1
        assert res.json()["counted_as_revenue"] is False


def test_a_policy_naming_this_source_counts_it():
    store = FakeRevenueSourceStore(_config(("stripe", "revenue-api")))
    with _client(revenue_sources=store) as (client, _ch):
        assert _post(client, _payload()).json()["counted_as_revenue"] is True


def test_an_unreadable_policy_reports_unknown_not_a_confident_yes():
    store = FakeRevenueSourceStore(raises=True)
    with _client(revenue_sources=store) as (client, ch):
        res = _post(client, _payload())
        assert res.status_code == 201
        assert res.json()["counted_as_revenue"] is None
        assert len(ch.events) == 1


def test_counts_as_revenue_mirrors_the_reader_defaults():
    assert counts_as_revenue(None, "revenue-api", "monetary") is True
    assert counts_as_revenue(None, "revenue-api", "mrr") is True
    assert counts_as_revenue(None, "revenue-api", "refund") is True
    assert counts_as_revenue(None, "revenue-api", "count") is False
    # include_mrr=False is the "our biller emits both for one subscription" case.
    assert counts_as_revenue(_config(None, include_mrr=False), "revenue-api", "mrr") is False
    assert counts_as_revenue(_config(None, include_mrr=False), "revenue-api", "monetary") is True
    # Source comparison is case-insensitive, matching lower(Source) in the reader.
    assert counts_as_revenue(_config(("Revenue-API",)), "revenue-api", "monetary") is True
