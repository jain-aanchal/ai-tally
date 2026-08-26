# SPDX-License-Identifier: Apache-2.0
"""The attribution stitcher runner: TouchStore mapping, writeback, and the skip path (CTO-200).

No ClickHouse: a fake client records the SQL / parameters / settings it is queried with and returns
canned ``result_rows``, and captures the inserts. What these tests assert is the acceptance list on
the ticket:

* the ClickHouse-backed ``TouchStore`` maps ``last_touch_index`` rows to the exact
  :class:`tally.stitcher.Touch` shape the stitcher expects, and answers ``query_last_touch`` from that
  one bounded load,
* a full run for a tenant with overlapping touches and value events WRITES ``attribution_records``
  (which is what lights /features up), and the written rows match the DDL column-for-column,
* a value event with no touch in its window is written to ``unattributed_events``, never guessed onto
  a trace,
* a tenant with no value events (or no touches) raises ``JobSkipped``, not an error,
* both ClickHouse loads are bounded: tenant-scoped, windowed, capped, and carrying the CTO-219
  server-side ceilings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tally.stitcher import AttributionConfidence

from gateway.scheduler import JobRegistry, JobSkipped
from gateway.stitcher_job import (
    JOB_NAME,
    ClickHouseAttributionWriter,
    ClickHouseTouchStore,
    ClickHouseValueEventSource,
    StitcherJob,
    _ATTRIBUTION_COLS,
    _UNATTRIBUTED_COLS,
    register_stitcher_job,
)

DDL = Path(__file__).resolve().parents[3] / "db" / "clickhouse" / "attribution.sql"

TENANT = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
USER_A = "a" * 64
USER_B = "b" * 64


class FakeCHClient:
    """Records queries and inserts; returns canned rows keyed by which table the SQL names."""

    def __init__(
        self,
        *,
        touch_rows: list[tuple] | None = None,
        event_rows: list[tuple] | None = None,
    ) -> None:
        self.queries: list[dict[str, object]] = []
        self.inserts: list[tuple[str, list, list[str]]] = []
        self._touch_rows = touch_rows or []
        self._event_rows = event_rows or []

    def query(self, sql: str, parameters=None, settings=None):  # noqa: ANN001, ANN202
        self.queries.append({"sql": sql, "parameters": parameters, "settings": settings})
        rows = self._touch_rows if "last_touch_index" in sql else self._event_rows

        class _R:
            result_rows = rows

        return _R()

    def insert(self, table: str, rows: list, column_names: list[str]) -> None:
        self.inserts.append((table, rows, column_names))


class FakeStore:
    """The slice of ``ClickHouseStore`` the job uses: a ``.client`` and a ``.close()``."""

    def __init__(self, client: FakeCHClient) -> None:
        self.client = client
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _touch_row(
    user_hash: str,
    feature_tag: str,
    *,
    ts: datetime,
    trace_id: str = "trace-1",
    cost: Decimal = Decimal("0.50000000"),
    key_version: str = "v1",
) -> tuple:
    # Matches the SELECT column order in ClickHouseTouchStore._SQL.
    return (user_hash, feature_tag, trace_id, ts, cost, key_version)


def _event_row(
    business_event_id: str,
    user_hash: str,
    *,
    event_name: str = "subscription_started",
    occurred_at: datetime,
    value_micro: int | None = 49_000_000,
    currency: str = "USD",
    value_type: str = "monetary",
) -> tuple:
    # Matches the SELECT column order in ClickHouseValueEventSource._SQL.
    return (business_event_id, event_name, user_hash, occurred_at, value_micro, currency, value_type)


def _job(store: FakeStore, **kw: object) -> StitcherJob:
    kw.setdefault("now", lambda: NOW)
    return StitcherJob(
        settings=None,  # type: ignore[arg-type]
        store_factory=lambda: store,
        **kw,  # type: ignore[arg-type]
    )


# --- TouchStore mapping --------------------------------------------------------------------------


def test_fixed_string_hash_bytes_are_decoded_not_repr_ed() -> None:
    """A FixedString(64) column comes back from clickhouse-connect as bytes. str(bytes) yields the
    b'...' repr, which is 68 chars, fails to bind back into FixedString(64), and never matches a
    touch. So an event whose hash arrives as bytes must still attribute against a touch with the same
    hash. This pins the boundary decode that a live run caught."""
    hash_bytes = (USER_A).encode("ascii")
    client = FakeCHClient(
        touch_rows=[_touch_row(hash_bytes, "chatbot", ts=NOW - timedelta(days=2))],
        event_rows=[_event_row("evt-b", hash_bytes, occurred_at=NOW - timedelta(days=1))],
    )
    store = FakeStore(client)
    _job(store)(TENANT)

    rows = {table: r for table, r, _c in client.inserts}["attribution_records"]
    row = dict(zip(_ATTRIBUTION_COLS, rows[0]))
    # Attributed (bytes on both sides decoded to the same 64-char hex, so they matched).
    assert row["BusinessEventId"] == "evt-b"
    assert row["FeatureTag"] == "chatbot"


def test_touch_store_maps_rows_to_the_stitcher_touch_shape() -> None:
    client = FakeCHClient(touch_rows=[_touch_row(USER_A, "chatbot", ts=NOW - timedelta(days=1))])
    store = FakeStore(client)
    touches = ClickHouseTouchStore(store, touch_lookback_days=60)
    loaded = touches.load(TENANT)

    assert loaded == 1
    assert touches.feature_tags() == {"chatbot"}
    got = touches.query_last_touch(
        tenant_id=TENANT,
        user_hashes={USER_A},
        feature_tag="chatbot",
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
    )
    assert got is not None
    assert got.user_hash == USER_A
    assert got.feature_tag == "chatbot"
    assert got.trace_id == "trace-1"
    # Decimal dollars -> integer micro-USD at the boundary.
    assert got.cost_micro_usd == 500_000
    # Naive ClickHouse DateTime64 is stamped UTC so the stitcher's aware comparisons never raise.
    assert got.ts.tzinfo is not None


def test_touch_store_query_respects_the_per_event_window() -> None:
    """The bulk load is wide; the precise per-event window is enforced in memory by query_last_touch."""
    client = FakeCHClient(touch_rows=[_touch_row(USER_A, "chatbot", ts=NOW - timedelta(days=45))])
    store = FakeStore(client)
    touches = ClickHouseTouchStore(store, touch_lookback_days=60)
    touches.load(TENANT)

    # Inside the load window (60d) but OUTSIDE a 30-day event window -> not a candidate.
    assert (
        touches.query_last_touch(
            tenant_id=TENANT,
            user_hashes={USER_A},
            feature_tag="chatbot",
            window_start=NOW - timedelta(days=30),
            window_end=NOW,
        )
        is None
    )


def test_touch_store_load_is_bounded_and_tenant_scoped() -> None:
    client = FakeCHClient(touch_rows=[])
    store = FakeStore(client)
    ClickHouseTouchStore(store, touch_lookback_days=60, cap=1234).load(TENANT)

    call = client.queries[0]
    sql = " ".join(str(call["sql"]).split())
    assert "TenantId = {tenant:String}" in sql
    # Newest-per-key collapse of the ReplacingMergeTree, not a trust in merges having happened.
    assert "argMax(LastTraceTs, UpdatedAt)" in sql
    assert "LastTraceTs >= subtractDays(now64(9), {days:UInt32})" in sql
    params = call["parameters"]
    assert isinstance(params, dict)
    assert params["tenant"] == TENANT
    assert params["days"] == 60
    assert params["cap"] == 1234
    settings = call["settings"]
    assert isinstance(settings, dict)
    assert settings["max_rows_to_read"] > 0
    assert settings["max_memory_usage"] > 0
    assert settings["max_execution_time"] > 0
    assert settings["read_overflow_mode"] == "throw"


def test_value_event_source_is_bounded_and_uses_final() -> None:
    client = FakeCHClient(event_rows=[])
    store = FakeStore(client)
    ClickHouseValueEventSource(lookback_days=30, cap=999).fetch(store, TENANT)

    call = client.queries[0]
    sql = " ".join(str(call["sql"]).split())
    # FINAL so a re-posted event (same BusinessEventId) is read once, not attributed twice.
    assert "business_events FINAL" in sql
    assert "TenantId = {tenant:String}" in sql
    assert "OccurredAt >= subtractDays(now64(9), {days:UInt32})" in sql
    params = call["parameters"]
    assert isinstance(params, dict)
    assert params["days"] == 30
    assert params["cap"] == 999
    assert isinstance(call["settings"], dict)


# --- a full run writes attribution_records -------------------------------------------------------


def test_a_run_with_overlapping_touch_and_event_writes_an_attribution_record() -> None:
    client = FakeCHClient(
        touch_rows=[_touch_row(USER_A, "chatbot", ts=NOW - timedelta(days=2))],
        event_rows=[_event_row("evt-1", USER_A, occurred_at=NOW - timedelta(days=1))],
    )
    store = FakeStore(client)
    _job(store)(TENANT)

    inserts = {table: (rows, cols) for table, rows, cols in client.inserts}
    assert "attribution_records" in inserts
    rows, cols = inserts["attribution_records"]
    assert cols == list(_ATTRIBUTION_COLS)
    assert len(rows) == 1
    row = dict(zip(cols, rows[0]))
    assert row["TenantId"] == TENANT
    assert row["BusinessEventId"] == "evt-1"
    assert row["FeatureTag"] == "chatbot"
    assert row["AttributedTraceId"] == "trace-1"
    assert row["ValueAmountMicro"] == 49_000_000
    # v1 attributes on a direct user-hash match -> 'direct' confidence, the Enum8 name.
    assert row["AttributionConfidence"] == AttributionConfidence.DIRECT.value
    assert row["AttributionModel"] == "last_touch_v1"
    assert row["LookbackWindowDays"] == 30
    # Decimal micro-USD -> Decimal dollars for the Decimal64(8) column.
    assert row["AttributedTraceCost"] == Decimal("500000") / Decimal(1_000_000)
    # The store is always closed.
    assert store.closed == 1


def test_the_written_columns_match_the_canonical_ddl() -> None:
    """A column added to attribution.sql but not to _ATTRIBUTION_COLS (or vice versa) shifts every
    value after it. Pin the insert column list against the DDL, exactly as the business_events test
    pins its own."""
    ddl = DDL.read_text()
    # The CREATE TABLE body for attribution_records.
    body = ddl.split("CREATE TABLE IF NOT EXISTS attribution_records", 1)[1].split(")\nENGINE", 1)[0]
    for col in _ATTRIBUTION_COLS:
        assert col in body, f"{col} missing from attribution_records DDL"
    for col in _UNATTRIBUTED_COLS:
        assert col in ddl


def test_value_event_with_no_touch_in_window_is_written_unattributed_not_guessed() -> None:
    client = FakeCHClient(
        # A touch exists, but for a DIFFERENT user, so the converting user has no touch.
        touch_rows=[_touch_row(USER_B, "chatbot", ts=NOW - timedelta(days=2))],
        event_rows=[_event_row("evt-lonely", USER_A, occurred_at=NOW - timedelta(days=1))],
    )
    store = FakeStore(client)
    _job(store)(TENANT)

    inserts = {table: rows for table, rows, _cols in client.inserts}
    # Nothing was attributed (no touch for USER_A), so no attribution_records insert happened.
    assert "attribution_records" not in inserts or inserts["attribution_records"] == []
    assert "unattributed_events" in inserts
    urows = inserts["unattributed_events"]
    assert len(urows) == 1
    urow = dict(zip(_UNATTRIBUTED_COLS, urows[0]))
    assert urow["BusinessEventId"] == "evt-lonely"
    assert urow["UserIdHash"] == USER_A
    assert urow["Reason"] == "no_trace_in_window"


# --- the skip path -------------------------------------------------------------------------------


def test_no_value_events_raises_jobskipped_not_an_error() -> None:
    client = FakeCHClient(
        touch_rows=[_touch_row(USER_A, "chatbot", ts=NOW - timedelta(days=2))],
        event_rows=[],
    )
    store = FakeStore(client)
    with pytest.raises(JobSkipped):
        _job(store)(TENANT)
    # A skip writes nothing and still closes the store.
    assert client.inserts == []
    assert store.closed == 1


def test_no_touches_raises_jobskipped() -> None:
    client = FakeCHClient(
        touch_rows=[],
        event_rows=[_event_row("evt-1", USER_A, occurred_at=NOW - timedelta(days=1))],
    )
    store = FakeStore(client)
    with pytest.raises(JobSkipped):
        _job(store)(TENANT)
    assert client.inserts == []


def test_a_clickhouse_failure_propagates_so_the_scheduler_records_failed() -> None:
    class Boom(FakeCHClient):
        def query(self, sql, parameters=None, settings=None):  # noqa: ANN001, ANN202
            raise RuntimeError("max_rows_to_read exceeded")

    store = FakeStore(Boom())
    with pytest.raises(RuntimeError):
        _job(store)(TENANT)
    # Even on failure the client is closed rather than leaked.
    assert store.closed == 1


# --- writer + registration -----------------------------------------------------------------------


def test_writer_reports_counts_and_skips_empty_inserts() -> None:
    client = FakeCHClient()
    store = FakeStore(client)
    written, unattributed = ClickHouseAttributionWriter().write(store, [], [])
    assert (written, unattributed) == (0, 0)
    # Nothing to write means no insert call at all.
    assert client.inserts == []


def test_register_stitcher_job_registers_the_hourly_job() -> None:
    registry = JobRegistry()
    job = register_stitcher_job(registry, settings=None)  # type: ignore[arg-type]
    assert job.name == JOB_NAME
    assert job.interval_s == 3600.0
    assert registry.get(JOB_NAME) is job


def test_rerun_writes_the_same_sort_key_so_replacingmergetree_dedupes() -> None:
    """Idempotency is the table's, not ours: two runs write the SAME (Tenant, BusinessEventId,
    FeatureTag) sort key, and StitchedAt (the version) advances, so FINAL collapses them."""
    rows = lambda: (  # noqa: E731
        [_touch_row(USER_A, "chatbot", ts=NOW - timedelta(days=2))],
        [_event_row("evt-1", USER_A, occurred_at=NOW - timedelta(days=1))],
    )
    first = FakeCHClient(touch_rows=rows()[0], event_rows=rows()[1])
    store1 = FakeStore(first)
    _job(store1)(TENANT)
    second = FakeCHClient(touch_rows=rows()[0], event_rows=rows()[1])
    store2 = FakeStore(second)
    _job(store2, now=lambda: NOW + timedelta(hours=1))(TENANT)

    r1 = dict(zip(_ATTRIBUTION_COLS, first.inserts[0][1][0]))
    r2 = dict(zip(_ATTRIBUTION_COLS, second.inserts[0][1][0]))
    key = ("TenantId", "BusinessEventId", "FeatureTag")
    assert tuple(r1[k] for k in key) == tuple(r2[k] for k in key)
    # The version column advances, so the later write wins on merge / FINAL.
    assert r2["StitchedAt"] > r1["StitchedAt"]
