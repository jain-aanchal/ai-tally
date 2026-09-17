# SPDX-License-Identifier: Apache-2.0
"""Per-tenant price overrides, wired into the live cost path (CTO-416).

These tests drive the REAL ingest path (POST /v1/batches into a fake ClickHouse, with a fake ledger
store standing in for Postgres) and assert the row that actually gets written, because the claim
being made is about the cost a customer sees, not about the ledger object in isolation. The ledger's
own semantics are covered in the SDK suite (``sdk/python/tests/test_overrides.py``).

The four claims, one test each:

* :func:`test_override_loaded_at_startup_changes_the_cost_a_span_is_enriched_with`: the whole point.
  Before this ticket the catalog was the seed and nothing else.
* :func:`test_failed_override_load_leaves_cost_unpriced_not_list_price`: the honesty invariant. A
  ledger we cannot read must NOT quietly re-price at list, because a contract rate is usually below
  list and the difference is spend the customer never incurred.
* :func:`test_superseding_entry_wins_over_the_version_it_supersedes`: re-pricing is a new version,
  never an update.
* :func:`test_tombstone_reverts_the_slot_to_the_public_catalog`: a withdrawal is a tombstone, and it
  falls back to the public rate rather than to nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from tally.overrides import OverrideRecord
from tally.pricing import PriceType, Unit, seed_catalog
from tally.schema import SpanFields, build_span_attributes

from gateway import app as app_module
from gateway.app import app
from gateway.config import get_settings
from gateway.mapping import COLUMNS
from gateway.price_overrides import TenantSpellings
from gateway.price_overrides import (
    MAX_PRICE_PER_UNIT,
    PriceOverrideError,
    normalize_currency,
    normalize_price_per_unit,
    normalize_price_type,
    normalize_reason,
    normalize_unit,
    resolve_tenant_spellings,
)

TENANT = "t-local"

#: Control-plane calls are service-token authed and always name their tenant explicitly.
HEADERS = {"X-Tenant-Id": TENANT}

# The span every test posts: 1,000,000 input + 1,000,000 output tokens, so a rate per million tokens
# reads straight off the assertion and nothing hides in a rounding.
ONE_MILLION = 1_000_000

# Public seed rates for openai/gpt-4o-mini (tally.pricing.seed_catalog): $0.15 in, $0.60 out per
# million. One million of each is therefore $0.75 at list.
LIST_PRICE = Decimal("0.75")

# The negotiated rates these tests write into the ledger: 10x cheaper, which is the shape of a real
# committed-use discount and is far enough from list that no rounding could confuse the two.
CONTRACT_INPUT = Decimal("0.015")
CONTRACT_OUTPUT = Decimal("0.060")
CONTRACT_PRICE = CONTRACT_INPUT + CONTRACT_OUTPUT  # $0.075


class FakeClickHouse:
    def __init__(self) -> None:
        self.spans: list[tuple] = []
        self._lock = threading.Lock()

    def insert_spans(self, rows: list[tuple]) -> int:
        with self._lock:
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


class FakeLedgerStore:
    """In-memory stand-in for :class:`gateway.price_overrides.PriceOverrideStore`.

    Mirrors the two properties the refresher depends on: records come back in ledger order, and a
    tenant's UUID also answers to its posted spelling. ``fail`` makes every read raise, which is the
    only thing the honesty test needs to reproduce (an unreachable Postgres, a missing migration and
    a permission error are all the same answer here, deliberately).
    """

    def __init__(
        self,
        records: list[OverrideRecord] | None = None,
        *,
        fail: bool = False,
        spellings: dict[str, list[str]] | None = None,
        ambiguous: list[str] | None = None,
    ) -> None:
        self.records = list(records or [])
        self.fail = fail
        self.spellings = spellings or {}
        self.ambiguous = ambiguous or []
        # Counted so a test can assert WHEN the ledger was read, which is the difference between
        # loading at startup and loading on the first batch that happens to arrive.
        self.loads = 0

    def load_all(self) -> list[OverrideRecord]:
        self.loads += 1
        if self.fail:
            raise RuntimeError("connection to server at \"postgres\" failed")
        return list(self.records)

    def load_tenant_spellings(self) -> TenantSpellings:
        if self.fail:
            raise RuntimeError("connection to server at \"postgres\" failed")
        return TenantSpellings(usable=dict(self.spellings), ambiguous=list(self.ambiguous))


def _record(
    price: Decimal | None,
    price_type: PriceType,
    *,
    version: int,
    supersedes: int | None = None,
    reason: str = "CTO-416 committed-use contract",
) -> OverrideRecord:
    return OverrideRecord(
        tenant_id=TENANT,
        provider="openai",
        model="gpt-4o-mini",
        price_type=price_type,
        version=version,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=price,
        valid_from=date(2020, 1, 1),
        valid_to=None,
        actor="amy@example.com",
        reason=reason,
        recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        supersedes=supersedes,
    )


def vars_of(record: OverrideRecord) -> dict:
    """Field dict for a slots-based frozen dataclass, so a test can restate one field."""
    return {f: getattr(record, f) for f in OverrideRecord.__dataclass_fields__}


def _contract_records() -> list[OverrideRecord]:
    return [
        _record(CONTRACT_INPUT, PriceType.INPUT, version=1),
        _record(CONTRACT_OUTPUT, PriceType.OUTPUT, version=1),
    ]


@contextmanager
def _client(
    ledger: FakeLedgerStore, *, ttl_s: float = 300.0
) -> Iterator[tuple[TestClient, FakeClickHouse]]:
    """A booted gateway whose override ledger is ``ledger`` and whose writes land in memory.

    The store is swapped at the FACTORY, the way the ClickHouse store is, so the lifespan does the
    real wiring: it builds the refresher, loads the ledger at startup and hands the ingest path the
    catalog it enriches against. Injecting a ready-made refresher afterwards would test this file's
    own wiring instead of the gateway's.

    The default TTL is long, so nothing here passes by accident on a reload the ingest path happened
    to trigger. :func:`test_a_price_written_on_another_replica_arrives_within_the_ttl` is the one
    test that shortens it, because that TTL is exactly what it is about.
    """
    store = FakeClickHouse()
    settings = get_settings()
    prev = (
        settings.ingest_buffered,
        settings.price_overrides_enabled,
        settings.price_overrides_refresh_ttl_s,
    )
    settings.ingest_buffered = False
    settings.price_overrides_enabled = True
    settings.price_overrides_refresh_ttl_s = ttl_s
    orig_ch = app_module.ClickHouseStore
    orig_ledger = app_module.PriceOverrideStore
    app_module.ClickHouseStore = lambda _settings: store  # type: ignore[assignment]
    app_module.PriceOverrideStore = lambda _settings: ledger  # type: ignore[assignment]
    try:
        with TestClient(app) as c:
            app.state.settings.require_api_key = False
            yield c, store
    finally:
        app_module.ClickHouseStore = orig_ch  # type: ignore[assignment]
        app_module.PriceOverrideStore = orig_ledger  # type: ignore[assignment]
        (
            settings.ingest_buffered,
            settings.price_overrides_enabled,
            settings.price_overrides_refresh_ttl_s,
        ) = prev
        # The catalog is process-wide state shared with every other test file, so a leaked override
        # pool or a leaked fail-closed flag would silently re-price their spans.
        app.state.catalog.clear_overrides()
        app.state.catalog.mark_overrides_loaded()


def _span() -> dict:
    attrs = build_span_attributes(
        SpanFields(
            system="openai",
            operation="chat",
            request_model="gpt-4o-mini",
            input_tokens=ONE_MILLION,
            output_tokens=ONE_MILLION,
            feature_tag="assistant",
        )
    )
    attrs.update({"trace_id": "trace-1", "span_id": "span-1"})
    return attrs


def _post(c: TestClient) -> None:
    r = c.post(
        "/v1/batches",
        json={"tenant_id": TENANT, "sdk_version": "test", "resource_spans": [_span()]},
    )
    assert r.status_code == 200, r.text


def _row(store: FakeClickHouse, index: int = -1) -> dict[str, object]:
    return dict(zip(COLUMNS, store.spans[index], strict=True))


# --- the wiring ----------------------------------------------------------------------------------


def test_override_loaded_at_startup_changes_the_cost_a_span_is_enriched_with() -> None:
    """End to end: a ledger entry, not an edit to tally.pricing, decides what the span costs."""
    ledger = FakeLedgerStore(_contract_records())
    with _client(ledger) as (c, store):
        # Read at BOOT, before this replica served anything. A replica that deferred the load to its
        # first batch would price that batch at list for every tenant holding a contract, and
        # nothing afterwards could identify those spans.
        assert ledger.loads == 1
        _post(c)

    row = _row(store)
    assert row["EstimatedCost"] == CONTRACT_PRICE
    assert row["EstimatedCost"] != LIST_PRICE
    assert row["CostSource"] == "estimated"
    # The version records WHICH rate priced it, so an invoice question has an audit answer.
    assert str(row["PriceCatalogVersion"]).startswith("override-")


def test_a_price_written_on_another_replica_arrives_within_the_ttl() -> None:
    """The refresh path: a rate this process never wrote still reaches its cost enrichment.

    This is what makes the feature work on more than one replica. The write-through refresh only
    covers the gateway that took the POST; every other one picks the change up here.
    """
    ledger = FakeLedgerStore([])
    with _client(ledger, ttl_s=0.0) as (c, store):
        _post(c)
        assert _row(store)["EstimatedCost"] == LIST_PRICE

        ledger.records = _contract_records()  # as if another replica had appended it
        _post(c)

    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE


def test_a_refresh_replaces_the_previous_pool_rather_than_stacking_on_it() -> None:
    """A reload must DROP what it loaded last time, or a withdrawn rate keeps pricing forever.

    Stacking looks harmless while rates only ever change value, and then a tombstone lands and the
    rate it withdrew is still in the pool with nothing to say so.
    """
    ledger = FakeLedgerStore(_contract_records())
    with _client(ledger, ttl_s=0.0) as (c, store):
        _post(c)
        assert _row(store)["EstimatedCost"] == CONTRACT_PRICE

        ledger.records = [
            *_contract_records(),
            _record(None, PriceType.INPUT, version=2, supersedes=1, reason="contract ended"),
            _record(None, PriceType.OUTPUT, version=2, supersedes=1, reason="contract ended"),
        ]
        _post(c)

    assert _row(store)["EstimatedCost"] == LIST_PRICE


def test_an_override_stored_under_the_uuid_prices_a_batch_posted_under_the_name() -> None:
    """The tenant-identity trap (CLAUDE.md): ingest enriches under the spelling the CALLER posted.

    The ledger keys on tenants.id, but an SDK configured with ``local-dev`` posts that. An override
    registered only under the UUID would price nothing for that tenant and say nothing about it: the
    span would just come back at list price.
    """
    uuid_tenant = "8f14e45f-ceea-467a-9a3c-2f0e4d1b7c60"
    records = [
        OverrideRecord(**{**vars_of(r), "tenant_id": uuid_tenant}) for r in _contract_records()
    ]
    ledger = FakeLedgerStore(records, spellings={uuid_tenant: [TENANT]})
    with _client(ledger) as (c, store):
        _post(c)  # posted as TENANT, the name

    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE


def test_a_name_two_tenants_share_carries_no_override() -> None:
    """An ambiguous spelling must not hand one tenant another tenant's contract rate."""
    rows = [
        ("11111111-1111-4111-8111-111111111111", "local-dev", "org_a"),
        ("22222222-2222-4222-8222-222222222222", "local-dev", None),
    ]
    resolved = resolve_tenant_spellings(rows)
    assert resolved.usable["11111111-1111-4111-8111-111111111111"] == ["org_a"]
    assert resolved.usable["22222222-2222-4222-8222-222222222222"] == []
    assert resolved.ambiguous == ["local-dev"]


def test_a_name_that_is_another_tenants_uuid_carries_no_override() -> None:
    """The cross-tenant leak: tenants.name is free text from the Clerk webhook (CTO-416 review).

    Counting alternates against each other only, the first version of this rule scored a name equal
    to ANOTHER tenant's canonical id as unique, so tenant B could name their org after tenant A's
    UUID, append a rate for themselves, and have it register under A's catalog key. A's own
    authenticated ingest then priced from B's contract. Authenticated, self-serve, production path.
    """
    victim = "11111111-1111-4111-8111-111111111111"
    attacker = "22222222-2222-4222-8222-222222222222"
    resolved = resolve_tenant_spellings(
        [(victim, "acme", None), (attacker, victim, "org_b")]
    )
    assert resolved.usable[attacker] == ["org_b"]
    assert victim in resolved.ambiguous
    # The victim keeps their own spellings; the attacker gains nothing.
    assert resolved.usable[victim] == ["acme"]


def test_a_dropped_spelling_is_reported_rather_than_silently_swallowed() -> None:
    """Dropping the alias is the safe choice, but it is not a no-op for the tenant who owns it.

    Their override stops applying to batches posted under that spelling, which from the outside is
    indistinguishable from the feature not working, and a third party can cause it by naming their
    own org after that tenant's name. So it is surfaced in the load status (and logged), not hidden.
    """
    ledger = RecordingLedgerStore(_contract_records(), ambiguous=["acme"])
    with _client(ledger) as (c, _store):
        load = c.get("/v1/tenant/price-overrides", headers=HEADERS).json()["load"]
    assert load["ambiguous_spellings"] == ["acme"]
    assert load["healthy"] is True  # degraded coverage, not a failed load


def test_a_uuid_shaped_name_carries_no_override_even_when_it_matches_no_tenant() -> None:
    """A spelling that merely parses as a UUID is refused too: that tenant may be created later,
    and this map is rebuilt on a schedule rather than on that event."""
    mine = "11111111-1111-4111-8111-111111111111"
    resolved = resolve_tenant_spellings(
        [(mine, "99999999-9999-4999-8999-999999999999", "org_mine")]
    )
    assert resolved.usable[mine] == ["org_mine"]
    assert resolved.ambiguous == ["99999999-9999-4999-8999-999999999999"]


def test_an_org_shaped_name_carries_no_override_either() -> None:
    """Symmetry with the UUID rule (CTO-416 round 2 nit).

    A Clerk org id is machine-assigned and may be handed to some tenant tomorrow, so a free-text
    NAME shaped like one pre-claims a spelling exactly the way a UUID-shaped name does. Refusing one
    shape and not the other was an asymmetry with no argument behind it.
    """
    mine = "11111111-1111-4111-8111-111111111111"
    resolved = resolve_tenant_spellings([(mine, "org_2someoneelse", "org_mine")])
    # The tenant's OWN clerk_org_id is machine-assigned and unique, so it is still usable.
    assert resolved.usable[mine] == ["org_mine"]
    assert resolved.ambiguous == ["org_2someoneelse"]


def test_public_catalog_still_prices_a_tenant_with_no_override() -> None:
    """The control for the test above: an empty ledger leaves list pricing exactly as it was."""
    with _client(FakeLedgerStore([])) as (c, store):
        _post(c)

    assert _row(store)["EstimatedCost"] == LIST_PRICE


# --- the honesty invariant -----------------------------------------------------------------------


def test_failed_override_load_leaves_cost_unpriced_not_list_price() -> None:
    """A ledger we cannot read must never be answered with the public list price.

    This is the invariant the whole feature is judged on. Falling back would report $0.75 of spend
    for a tenant whose contract says $0.075, and the number would look exactly like a real one.
    """
    with _client(FakeLedgerStore(_contract_records(), fail=True)) as (c, store):
        _post(c)

    row = _row(store)
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "unpriced"
    assert row["PriceCatalogVersion"] == ""


def test_failed_override_load_is_visible_on_readyz() -> None:
    """The failure is a health signal, not only a log line: blank costs alone look like no traffic."""
    with _client(FakeLedgerStore(_contract_records(), fail=True)) as (c, _store):
        body = c.get("/readyz").json()
        assert body["degraded"] == ["price_overrides"]
        assert body["price_overrides"]["healthy"] is False
        assert body["price_overrides"]["error"]


def test_a_failed_override_load_does_not_take_the_replica_out_of_rotation() -> None:
    """Degraded pricing must not stop INGEST (CTO-416 review).

    ``/readyz`` answering 503 pulls the replica from the load balancer, so folding the ledger into
    ``ready`` traded blank cost columns for dropped customer telemetry, on every replica at once and
    with no self-healing. The likely trigger is mundane: the flag on before migration 0035 has been
    applied by hand to a running stack, against a perfectly healthy Postgres.
    """
    class HealthyAuth:
        """Stands in for the Postgres-backed auth ping, so this asserts the PRICE check alone."""

        def ping(self) -> bool:
            return True

    with _client(FakeLedgerStore(_contract_records(), fail=True)) as (c, store):
        real_auth = app.state.auth
        app.state.auth = HealthyAuth()
        try:
            readyz = c.get("/readyz")
        finally:
            app.state.auth = real_auth
        body = readyz.json()
        # Dependencies are healthy, so the replica is READY even though pricing is degraded.
        assert readyz.status_code == 200, body
        assert body["ready"] is True
        assert body["degraded"] == ["price_overrides"]
        assert "price_overrides" not in body["checks"]
        _post(c)  # and ingest still accepts the batch

    assert len(store.spans) == 1
    assert _row(store)["CostSource"] == "unpriced"


def test_a_recovered_ledger_prices_again_without_a_restart() -> None:
    """Fail-closed self-heals on the next successful refresh, which is why it is safe to be strict."""
    ledger = FakeLedgerStore(_contract_records(), fail=True)
    with _client(ledger) as (c, store):
        _post(c)
        assert _row(store)["CostSource"] == "unpriced"

        ledger.fail = False
        r = c.post("/v1/tenant/price-overrides/refresh", headers=HEADERS)
        assert r.status_code == 200, r.text
        _post(c)

    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE


# --- versioning and tombstones -------------------------------------------------------------------


def test_superseding_entry_wins_over_the_version_it_supersedes() -> None:
    """Re-pricing appends a new version; the newer one is what the cost path applies."""
    records = [
        *_contract_records(),
        _record(Decimal("0.030"), PriceType.INPUT, version=2, supersedes=1, reason="renegotiated"),
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post(c)

    # v2 input (0.030) plus the untouched v1 output (0.060), not the superseded 0.015.
    assert _row(store)["EstimatedCost"] == Decimal("0.090")


def test_tombstone_reverts_the_slot_to_the_public_catalog() -> None:
    """A revocation is a tombstone, and the slot falls back to list price rather than to blank."""
    records = [
        *_contract_records(),
        _record(None, PriceType.INPUT, version=2, supersedes=1, reason="contract ended"),
        _record(None, PriceType.OUTPUT, version=2, supersedes=1, reason="contract ended"),
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post(c)

    assert _row(store)["EstimatedCost"] == LIST_PRICE


def test_a_tombstone_on_one_slot_leaves_the_other_slot_overridden() -> None:
    """The ledger resolves per slot, so withdrawing the input rate must not withdraw the output one."""
    records = [
        *_contract_records(),
        _record(None, PriceType.INPUT, version=2, supersedes=1, reason="input back to list"),
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post(c)

    # List input ($0.15) plus the still-active contract output ($0.060).
    assert _row(store)["EstimatedCost"] == Decimal("0.21")


# --- the control plane ---------------------------------------------------------------------------


class RecordingLedgerStore(FakeLedgerStore):
    """Adds the write half, so the endpoint tests exercise append-only versioning without Postgres."""

    def history(self, tenant_id: str) -> list[OverrideRecord]:
        if self.fail:
            raise RuntimeError("connection to server at \"postgres\" failed")
        return [r for r in self.records if r.tenant_id == TENANT]

    def append(
        self,
        tenant_id: str,
        *,
        provider: str,
        model: str,
        price_type: PriceType,
        price_per_unit: Decimal | None,
        actor: str,
        reason: str,
        unit: Unit = Unit.PER_MILLION_TOKENS,
        valid_from: date | None = None,
        valid_to: date | None = None,
        currency: str = "USD",
    ) -> OverrideRecord:
        prior = [
            r.version
            for r in self.records
            if (r.provider, r.model, r.price_type) == (provider, model, price_type)
        ]
        record = OverrideRecord(
            tenant_id=TENANT,
            provider=provider,
            model=model,
            price_type=price_type,
            version=(max(prior) if prior else 0) + 1,
            unit=unit,
            price_per_unit=price_per_unit,
            valid_from=valid_from or date(2026, 9, 1),
            valid_to=valid_to,
            actor=actor,
            reason=reason,
            recorded_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
            supersedes=max(prior) if prior else None,
            currency=currency,
        )
        self.records.append(record)
        return record


def test_appending_a_price_through_the_control_plane_prices_the_next_span() -> None:
    """The ticket in one test: a price added by a WRITE, with no deploy and no restart."""
    ledger = RecordingLedgerStore([])
    with _client(ledger) as (c, store):
        _post(c)
        assert _row(store)["EstimatedCost"] == LIST_PRICE

        for price_type, rate in (("input", "0.015"), ("output", "0.060")):
            r = c.post(
                "/v1/tenant/price-overrides",
                json={
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "price_type": price_type,
                    "price_per_unit": rate,
                    "actor": "amy@example.com",
                    "reason": "CTO-416 committed-use contract",
                },
                headers=HEADERS,
            )
            assert r.status_code == 200, r.text
            assert r.json()["applied"] is True

        _post(c)

    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE


def test_the_ledger_read_shows_active_rates_and_the_full_audit_trail() -> None:
    """``history=true`` returns tombstones and superseded versions: that is what makes it an audit."""
    records = [
        *_contract_records(),
        _record(None, PriceType.INPUT, version=2, supersedes=1, reason="contract ended"),
    ]
    with _client(RecordingLedgerStore(records)) as (c, _store):
        active = c.get("/v1/tenant/price-overrides", headers=HEADERS).json()
        full = c.get(
            "/v1/tenant/price-overrides", params={"history": "true"}, headers=HEADERS
        ).json()

    assert [o["price_type"] for o in active["overrides"]] == ["output"]
    assert active["configured"] is True
    assert "history" not in active
    assert len(full["history"]) == 3
    assert [h["revoked"] for h in full["history"]] == [False, False, True]
    assert all(h["actor"] and h["reason"] for h in full["history"])


def test_a_revocation_is_a_tombstone_rather_than_a_delete() -> None:
    """There is no delete path: withdrawing a rate appends a versioned tombstone."""
    ledger = RecordingLedgerStore(_contract_records())
    with _client(ledger) as (c, _store):
        r = c.post(
            "/v1/tenant/price-overrides",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "price_type": "input",
                "revoke": True,
                "actor": "amy@example.com",
                "reason": "contract ended",
            },
            headers=HEADERS,
        )
        assert r.status_code == 200, r.text
        entry = r.json()["entry"]

    assert entry["revoked"] is True
    assert entry["price_per_unit"] is None
    assert entry["version"] == 2 and entry["supersedes"] == 1
    # The superseded rate is still there. That is what lets a past invoice be explained.
    assert len(ledger.records) == 3


def test_an_unreadable_ledger_is_a_503_not_an_empty_list() -> None:
    """Answering [] would claim the tenant has negotiated nothing, which we cannot know here."""
    with _client(RecordingLedgerStore(_contract_records(), fail=True)) as (c, _store):
        assert c.get("/v1/tenant/price-overrides", headers=HEADERS).status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"price_per_unit": 0.07},  # float dollars
        {"price_per_unit": "-1"},
        {"price_per_unit": str(MAX_PRICE_PER_UNIT + 1)},
        {"price_type": "wholesale"},
        {"actor": ""},
        {"reason": ""},
        {"valid_from": "01/09/2026"},
    ],
)
def test_a_malformed_price_is_refused_at_the_boundary(body: dict) -> None:
    """Every one of these would otherwise land as a 503 carrying a constraint name, or worse, stick."""
    payload = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "price_type": "input",
        "price_per_unit": "0.015",
        "actor": "amy@example.com",
        "reason": "CTO-416 contract",
    }
    payload.update(body)
    with _client(RecordingLedgerStore([])) as (c, _store):
        r = c.post("/v1/tenant/price-overrides", json=payload, headers=HEADERS)
    assert r.status_code == 422, r.text


# --- normalizers ---------------------------------------------------------------------------------


def test_a_float_rate_is_refused_even_when_it_looks_harmless() -> None:
    """0.07 is not 0.07 in binary. Money that arrives as a float has already lost precision."""
    with pytest.raises(PriceOverrideError):
        normalize_price_per_unit(0.07)
    assert normalize_price_per_unit("0.07") == Decimal("0.07")
    assert normalize_price_per_unit(3) == Decimal(3)


def test_rates_stay_decimal_end_to_end() -> None:
    assert isinstance(normalize_price_per_unit("2.40"), Decimal)
    assert normalize_price_type("INPUT") is PriceType.INPUT
    assert normalize_unit(None) is Unit.PER_MILLION_TOKENS
    with pytest.raises(PriceOverrideError):
        normalize_unit("per_fortnight")
    with pytest.raises(PriceOverrideError):
        normalize_reason("   ")


# --- date windows: the rate in force, not the newest row (CTO-416 review) ------------------------


def _dated(
    price: Decimal | None,
    price_type: PriceType,
    *,
    version: int,
    valid_from: date,
    supersedes: int | None = None,
) -> OverrideRecord:
    record = _record(price, price_type, version=version, supersedes=supersedes)
    return OverrideRecord(**{**vars_of(record), "valid_from": valid_from})


def _post_at(c: TestClient, when: date) -> None:
    """Post the standard span stamped with a specific date, the way a backfill would."""
    span = _span()
    span["timestamp_ns"] = int(
        datetime(when.year, when.month, when.day, 12, tzinfo=timezone.utc).timestamp() * 1e9
    )
    r = c.post(
        "/v1/batches",
        json={"tenant_id": TENANT, "sdk_version": "test", "resource_spans": [span]},
    )
    assert r.status_code == 200, r.text


def test_a_rate_negotiated_in_advance_does_not_hide_the_one_in_force() -> None:
    """The defect this catches silently TRIPLED a contract tenant's reported cost.

    Scheduling next quarter's rate is the documented use of valid_from. Materializing only the
    slot's newest entry meant the rate actually in force today was never handed to the catalog, so
    the lookup fell through to the PUBLIC price until the new window opened.
    """
    today = datetime.now(timezone.utc).date()
    in_force = today - timedelta(days=30)
    future = today + timedelta(days=90)
    records = [
        _dated(CONTRACT_INPUT, PriceType.INPUT, version=1, valid_from=in_force),
        _dated(CONTRACT_OUTPUT, PriceType.OUTPUT, version=1, valid_from=in_force),
        # Negotiated now, starts next quarter. Newest entry for both slots.
        _dated(Decimal("0.005"), PriceType.INPUT, version=2, valid_from=future, supersedes=1),
        _dated(Decimal("0.020"), PriceType.OUTPUT, version=2, valid_from=future, supersedes=1),
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post(c)

    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE  # today's rate, not the future one
    assert _row(store)["EstimatedCost"] != LIST_PRICE  # and emphatically not list


def test_a_span_is_priced_on_its_own_date_not_todays() -> None:
    """Historical recompute, which is the reason the ledger is versioned and windowed at all.

    A backfilled or late span priced against today's window is the same class of wrong as pricing
    it at list: it reports a number the customer was never charged. docs/price-overrides.md promises
    the span's date, and that promise is meant to be citable to a customer about a bill.
    """
    today = datetime.now(timezone.utc).date()
    march = today - timedelta(days=120)
    records = [
        _dated(Decimal("0.030"), PriceType.INPUT, version=1, valid_from=march - timedelta(days=10)),
        _dated(Decimal("0.120"), PriceType.OUTPUT, version=1, valid_from=march - timedelta(days=10)),
        _dated(CONTRACT_INPUT, PriceType.INPUT, version=2, valid_from=today, supersedes=1),
        _dated(CONTRACT_OUTPUT, PriceType.OUTPUT, version=2, valid_from=today, supersedes=1),
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post_at(c, march)
        _post(c)

    assert dict(zip(COLUMNS, store.spans[0], strict=True))["EstimatedCost"] == Decimal("0.150")
    assert dict(zip(COLUMNS, store.spans[1], strict=True))["EstimatedCost"] == CONTRACT_PRICE


def test_a_tombstone_closes_the_slot_at_its_date_and_kills_windows_after_it() -> None:
    """A revocation says there is no override from its date ONWARD (CTO-416 round 2).

    Both halves matter and the first version of this rule got both wrong: it applied the revocation
    the moment it was filed regardless of date, and it left a window scheduled before the revocation
    alive to resume afterwards.
    """
    today = datetime.now(timezone.utc).date()
    records = [
        _dated(CONTRACT_INPUT, PriceType.INPUT, version=1, valid_from=today - timedelta(days=30)),
        _dated(CONTRACT_OUTPUT, PriceType.OUTPUT, version=1, valid_from=today - timedelta(days=30)),
        # Scheduled first, for a date after the revocation that is filed next. The revocation says
        # the slot ends, so this must not resume.
        _dated(Decimal("0.005"), PriceType.INPUT, version=2, valid_from=today + timedelta(days=30)),
        _dated(None, PriceType.INPUT, version=3, valid_from=today, supersedes=2),
    ]
    with _client(RecordingLedgerStore(records), ttl_s=0.0) as (c, store):
        _post(c)  # today: input ended (list), output still on contract
        # Asserted against the catalog rather than by posting a future-dated span, because the skew
        # clamp (correctly) rewrites a timestamp from the future to server time.
        after = app.state.catalog.lookup(
            "openai",
            "gpt-4o-mini",
            PriceType.INPUT,
            at=today + timedelta(days=60),
            tenant_id=TENANT,
        )
        before = app.state.catalog.lookup(
            "openai",
            "gpt-4o-mini",
            PriceType.INPUT,
            at=today - timedelta(days=10),
            tenant_id=TENANT,
        )
        listed = c.get("/v1/tenant/price-overrides", headers=HEADERS).json()

    assert dict(zip(COLUMNS, store.spans[0], strict=True))["EstimatedCost"] == Decimal("0.21")
    # Nothing OVERRIDES this slot after the end date: the lookup falls through to the public rate.
    assert after is not None and after.version.startswith("seed-")
    # But a span from BEFORE the end date still prices at the contract rate: the window was closed,
    # not erased, so an invoice from last month is still explainable.
    assert before is not None and before.price_per_unit == CONTRACT_INPUT
    # The GET separates the two questions a customer asks: the input slot's latest entry is the
    # scheduled v3, while what can still price a span is the closed input window and the output one.
    # The GET separates the two questions a customer asks: the input slot's latest entry is the
    # tombstone (so no live rate), while what can still price a span is the CLOSED input window and
    # the untouched output one.
    assert sorted((o["price_type"], o["version"]) for o in listed["overrides"]) == [("output", 1)]
    assert sorted((o["price_type"], o["version"]) for o in listed["in_force"]) == [
        ("input", 1),
        ("output", 1),
    ]


def test_a_revocation_filed_in_advance_does_not_reprice_today() -> None:
    """End to end through the documented endpoint, which is how the defect was reachable.

    An operator scheduling "this contract ends on 1 January" got 200 applied: true and the tenant
    silently started over-reporting at public list months early.
    """
    today = datetime.now(timezone.utc).date()
    ends_on = today + timedelta(days=150)
    ledger = RecordingLedgerStore(
        [
            _dated(CONTRACT_INPUT, PriceType.INPUT, version=1, valid_from=today - timedelta(days=1)),
            _dated(
                CONTRACT_OUTPUT, PriceType.OUTPUT, version=1, valid_from=today - timedelta(days=1)
            ),
        ]
    )
    with _client(ledger, ttl_s=0.0) as (c, store):
        for price_type in ("input", "output"):
            r = c.post(
                "/v1/tenant/price-overrides",
                json={
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "price_type": price_type,
                    "revoke": True,
                    "valid_from": ends_on.isoformat(),
                    "actor": "amy@example.com",
                    "reason": "contract ends at the end of the term",
                },
                headers=HEADERS,
            )
            assert r.status_code == 200, r.text
            assert r.json()["entry"]["revoked"] is True
        _post(c)
        after = app.state.catalog.lookup(
            "openai",
            "gpt-4o-mini",
            PriceType.INPUT,
            at=ends_on + timedelta(days=1),
            tenant_id=TENANT,
        )

    # Today is still inside the contract: the scheduled end must not reprice it.
    assert _row(store)["EstimatedCost"] == CONTRACT_PRICE
    # And it does take effect on its date: past the end, the public rate applies again.
    assert after is not None and after.version.startswith("seed-")


def test_a_revocation_may_not_carry_a_valid_to() -> None:
    """A tombstone closes a window; it does not open one, so a second date is meaningless."""
    with _client(RecordingLedgerStore([])) as (c, _store):
        r = c.post(
            "/v1/tenant/price-overrides",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "price_type": "input",
                "revoke": True,
                "valid_from": "2027-01-01",
                "valid_to": "2027-06-01",
                "actor": "amy@example.com",
                "reason": "contract ends",
            },
            headers=HEADERS,
        )
    assert r.status_code == 422, r.text
    assert "valid_to" in r.text


def test_a_backdated_revocation_ends_the_override_where_it_says() -> None:
    """"The contract ended on the 1st", filed later, must not keep pricing after that date."""
    today = datetime.now(timezone.utc).date()
    ended = today - timedelta(days=10)
    records = [
        _dated(CONTRACT_INPUT, PriceType.INPUT, version=1, valid_from=today - timedelta(days=60)),
        _dated(CONTRACT_OUTPUT, PriceType.OUTPUT, version=1, valid_from=today - timedelta(days=60)),
        _dated(None, PriceType.INPUT, version=2, valid_from=ended, supersedes=1),
        _dated(None, PriceType.OUTPUT, version=2, valid_from=ended, supersedes=1),
    ]
    with _client(RecordingLedgerStore(records), ttl_s=0.0) as (c, store):
        _post(c)  # today, after the end date
        _post_at(c, today - timedelta(days=30))  # before it

    assert dict(zip(COLUMNS, store.spans[0], strict=True))["EstimatedCost"] == LIST_PRICE
    assert dict(zip(COLUMNS, store.spans[1], strict=True))["EstimatedCost"] == CONTRACT_PRICE


# --- interaction with the billing mode (CTO-417) -------------------------------------------------


def test_a_subscription_span_is_not_priced_by_an_override_either() -> None:
    """Billing mode is decided BEFORE any rate is looked up, contract or public (CTO-417 + CTO-416).

    A subscription-billed call has no per-call price to know, so a negotiated rate is as wrong an
    answer as a list rate. The two features meet here and the order matters: mode first, then the
    tenant's contract, then the public catalog. CostSource says 'subscription' rather than
    'unpriced', because "there is no per-call price" is a different fact from "we do not have one".
    """
    span = _span()
    span["gen_ai.cost.billing_mode"] = "subscription"
    with _client(FakeLedgerStore(_contract_records())) as (c, store):
        r = c.post(
            "/v1/batches",
            json={"tenant_id": TENANT, "sdk_version": "test", "resource_spans": [span]},
        )
        assert r.status_code == 200, r.text

    row = _row(store)
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "subscription"
    assert row["EstimatedCost"] != CONTRACT_PRICE


def test_a_subscription_span_stays_subscription_when_the_ledger_is_unreadable() -> None:
    """Fail-closed pricing must not relabel a fact we DO know as one we do not."""
    span = _span()
    span["gen_ai.cost.billing_mode"] = "subscription"
    with _client(FakeLedgerStore(_contract_records(), fail=True)) as (c, store):
        r = c.post(
            "/v1/batches",
            json={"tenant_id": TENANT, "sdk_version": "test", "resource_spans": [span]},
        )
        assert r.status_code == 200, r.text

    assert _row(store)["CostSource"] == "subscription"


# --- the refresh must never expose a half-built pool ---------------------------------------------


def test_a_refresh_swaps_the_pool_in_one_step_and_never_empties_it_in_place() -> None:
    """Rebuilding in place priced contract tenants at LIST for the length of every refresh.

    ``refresh`` runs on a worker thread while the ingest path keeps enriching and readers hold no
    lock, so any window in which the pool is empty and still flagged trustworthy is a window in
    which a lookup falls through to the public catalog. Once per TTL window, forever, not only on
    failure. This asserts the mechanism rather than trying to hit a microsecond race: the pool is
    installed with one assignment and is never cleared in place on the success path.
    """
    ledger = FakeLedgerStore(_contract_records())
    with _client(ledger) as (c, _store):
        catalog = app.state.catalog
        calls: list[str] = []
        original_clear = catalog.clear_overrides
        original_replace = catalog.replace_overrides
        catalog.clear_overrides = lambda: (calls.append("clear"), original_clear())[1]
        catalog.replace_overrides = lambda pool: (
            calls.append("replace"),
            original_replace(pool),
        )[1]
        try:
            assert c.post("/v1/tenant/price-overrides/refresh", headers=HEADERS).status_code == 200
        finally:
            catalog.clear_overrides = original_clear
            catalog.replace_overrides = original_replace

    assert calls == ["replace"]


def test_a_failed_load_flags_the_catalog_before_it_drops_the_pool() -> None:
    """Ordering, at the layer that owns it: the reverse order leaves an empty pool looking usable."""
    catalog = seed_catalog()
    catalog.add_override(TENANT, _contract_records()[0].to_price_entry())
    catalog.mark_overrides_unavailable("postgres unreachable")
    assert catalog.overrides_unavailable == "postgres unreachable"
    assert catalog.lookup("openai", "gpt-4o-mini", PriceType.INPUT, tenant_id=TENANT) is None


# --- units that cannot price the tier -------------------------------------------------------------


def test_a_per_gb_rate_on_a_token_tier_is_refused_at_the_boundary() -> None:
    """It used to be accepted, stored, and then price every matching span at a confident ZERO.

    ``_line`` has no arithmetic for per-GB, so the cost came back 0 carrying a real catalog version:
    the span read as priced rather than as unpriced, which is the fabricated number the Nullable
    cost columns exist to prevent.
    """
    with _client(RecordingLedgerStore([])) as (c, _store):
        r = c.post(
            "/v1/tenant/price-overrides",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "price_type": "input",
                "unit": "per_gb",
                "price_per_unit": "0.015",
                "actor": "amy@example.com",
                "reason": "CTO-416 contract",
            },
            headers=HEADERS,
        )
        assert r.status_code == 422, r.text
        assert "per_gb" in r.text

        # And the endpoint does not advertise the pairing it would refuse.
        listed = c.get("/v1/tenant/price-overrides", headers=HEADERS).json()
    assert listed["available_units"]["input"] == ["per_million_tokens"]
    assert listed["available_units"]["tool_call"] == ["per_call"]


def test_a_unit_that_cannot_price_its_tier_is_a_miss_not_a_zero() -> None:
    """Belt and braces below the boundary: a bad pairing already in the table prices NOTHING."""
    records = [
        OverrideRecord(**{**vars_of(r), "unit": Unit.PER_GB}) for r in _contract_records()
    ]
    with _client(FakeLedgerStore(records)) as (c, store):
        _post(c)

    row = _row(store)
    assert row["EstimatedCost"] == LIST_PRICE  # the public rate still applies, and it is not zero
    assert row["CostSource"] == "estimated"


def test_a_non_usd_rate_is_refused() -> None:
    """Nothing converts currency, so a EUR contract would be reported as USD spend."""
    with _client(RecordingLedgerStore([])) as (c, _store):
        r = c.post(
            "/v1/tenant/price-overrides",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "price_type": "input",
                "price_per_unit": "0.015",
                "currency": "EUR",
                "actor": "amy@example.com",
                "reason": "CTO-416 contract",
            },
            headers=HEADERS,
        )
    assert r.status_code == 422, r.text
    with pytest.raises(PriceOverrideError):
        normalize_currency("eur")
    assert normalize_currency(None) == "USD"


def test_an_audit_field_may_not_carry_control_characters() -> None:
    """actor and reason are read back into logs and a dashboard (CTO-408 class)."""
    with _client(RecordingLedgerStore([])) as (c, _store):
        r = c.post(
            "/v1/tenant/price-overrides",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "price_type": "input",
                "price_per_unit": "0.015",
                "actor": "amy@example.com",
                "reason": "line one\nWARNING forged line two",
            },
            headers=HEADERS,
        )
    assert r.status_code == 422, r.text


# --- the disabled path and the control-plane gate --------------------------------------------------


def test_with_the_feature_disabled_pricing_is_untouched_and_an_append_is_not_applied() -> None:
    """Flag off must be byte-identical to before this landed, and must SAY the write is not live."""
    ledger = RecordingLedgerStore(_contract_records())
    store = FakeClickHouse()
    settings = get_settings()
    prev = settings.ingest_buffered, settings.price_overrides_enabled
    settings.ingest_buffered = False
    settings.price_overrides_enabled = False
    orig_ch, orig_ledger = app_module.ClickHouseStore, app_module.PriceOverrideStore
    app_module.ClickHouseStore = lambda _settings: store
    app_module.PriceOverrideStore = lambda _settings: ledger
    try:
        with TestClient(app) as c:
            app.state.settings.require_api_key = False
            _post(c)
            assert ledger.loads == 0  # nothing read the ledger at all
            posted = c.post(
                "/v1/tenant/price-overrides",
                json={
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "price_type": "input",
                    "price_per_unit": "0.015",
                    "actor": "amy@example.com",
                    "reason": "CTO-416 contract",
                },
                headers=HEADERS,
            ).json()
    finally:
        app_module.ClickHouseStore, app_module.PriceOverrideStore = orig_ch, orig_ledger
        settings.ingest_buffered, settings.price_overrides_enabled = prev
        app.state.catalog.clear_overrides()
        app.state.catalog.mark_overrides_loaded()

    assert dict(zip(COLUMNS, store.spans[0], strict=True))["EstimatedCost"] == LIST_PRICE
    # Stored and audited, but pricing nothing. Saying "applied" here is the control-plane version of
    # a confident wrong number.
    assert posted["applied"] is False
    assert posted["load"]["enabled"] is False


def test_the_price_override_endpoints_require_the_service_token() -> None:
    """Control-plane only: the web server is the sole caller, same gate as every /v1/tenant route."""
    ledger = RecordingLedgerStore([])
    with _client(ledger) as (c, _store):
        settings = app.state.settings
        settings.require_api_key = True
        settings.gateway_service_token = "s3rvice"
        try:
            unauthed = [
                c.get("/v1/tenant/price-overrides", headers=HEADERS),
                c.post("/v1/tenant/price-overrides", headers=HEADERS, json={}),
                c.post("/v1/tenant/price-overrides/refresh", headers=HEADERS),
            ]
            for r in unauthed:
                assert r.status_code == 401, r.text
            # And the right token gets in, so the assertion above is about auth and not about a
            # route that happens to reject everything.
            authed = {**HEADERS, "Authorization": "Bearer s3rvice"}
            assert c.get("/v1/tenant/price-overrides", headers=authed).status_code == 200
        finally:
            settings.require_api_key = False
            settings.gateway_service_token = ""
