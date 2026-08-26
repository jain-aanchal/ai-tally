# SPDX-License-Identifier: Apache-2.0
"""/v1/tenant/revenue-uploads — round trip, idempotent re-upload, per-line rejection (CTO-198).

The fake ClickHouse store below mirrors the real one's contract closely enough to prove the
property that matters: a second upload of the same period REPLACES it. If the delete half of the
write were dropped, or the ids stopped being derived, ``test_reupload_replaces_the_period`` fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.revenue_upload import PeriodSnapshot, RevenueUploadError, UploadSnapshotRow

T = "t-acme"
UTC = timezone.utc

CSV = (
    "account_id,period,amount,currency\n"
    "acct_1001,2026-08,12500.00,USD\n"
    "acct_1002,2026-08,4200.50,USD\n"
)


class FakeClickHouseStore:
    """Keeps the business_events rows in a dict keyed the way ClickHouse orders the table."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}
        self.fail_next_write = False

    def insert_business_events(self, tenant_id: str, events: list) -> int:
        if self.fail_next_write:
            raise RuntimeError("clickhouse down")
        for e in events:
            self.rows[(tenant_id, e.business_event_id)] = e
        return len(events)

    def delete_business_events_by_id_prefix(
        self, tenant_id: str, source: str, id_prefix: str
    ) -> None:
        for key in [
            k
            for k, v in self.rows.items()
            if k[0] == tenant_id and k[1].startswith(id_prefix) and v.source == source
        ]:
            del self.rows[key]

    def close(self) -> None:
        """The app's lifespan shutdown closes the store; nothing to do for the fake."""


class FakeUploadStore:
    """In-memory stand-in with the real table's (tenant_id, period) primary key."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], UploadSnapshotRow] = {}
        self.fail_next_write = False

    def list(self, tenant_id: str) -> list[UploadSnapshotRow]:
        return sorted(
            (v for (t, _), v in self.rows.items() if t == tenant_id),
            key=lambda r: r.period,
            reverse=True,
        )

    def record(
        self,
        tenant_id: str,
        snapshot: PeriodSnapshot,
        *,
        filename: str | None,
        uploaded_by: str | None,
    ) -> UploadSnapshotRow:
        if self.fail_next_write:
            raise RuntimeError("postgres down")
        row = UploadSnapshotRow(
            period=snapshot.period,
            source="csv_upload",
            account_count=snapshot.account_count,
            total_amount_micro=snapshot.total_amount_micro,
            currency=snapshot.currency,
            filename=filename,
            uploaded_at=datetime.now(tz=UTC),
            uploaded_by=uploaded_by,
        )
        self.rows[(tenant_id, snapshot.period)] = row
        return row

    def delete(self, tenant_id: str, period: str) -> bool:
        return self.rows.pop((tenant_id, period), None) is not None


class FakeRevenueSourceStore:
    """Only ``get`` is exercised — the upload's advisory narrowing note reads it."""

    def __init__(self, config: object = None) -> None:
        self.config = config

    def get(self, tenant_id: str) -> object:
        return self.config


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        app.state.store = FakeClickHouseStore()
        app.state.revenue_uploads = FakeUploadStore()
        app.state.tenant_revenue_sources = FakeRevenueSourceStore()
        yield c


def _upload(client: TestClient, csv: str = CSV, **extra):
    return client.post(
        "/v1/tenant/revenue-uploads",
        json={"csv": csv, "filename": "aug.csv", "uploaded_by": "finance@acme.test", **extra},
        headers={"X-Tenant-Id": T},
    )


def test_upload_writes_events_and_a_manifest_row(client: TestClient) -> None:
    r = _upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted_rows"] == 2
    assert len(body["snapshots"]) == 1
    snap = body["snapshots"][0]
    assert snap["period"] == "2026-08"
    assert snap["account_count"] == 2
    assert snap["total_amount_micro"] == 16_700_500_000
    assert snap["uploaded_at"]  # the "as of" the staleness badge is derived from
    assert len(app.state.store.rows) == 2


def test_uploaded_events_look_like_every_other_revenue_source(client: TestClient) -> None:
    # Same table, same ValueType discriminator, account hash in both identity columns — nothing
    # downstream should need to know these arrived by spreadsheet.
    _upload(client)
    event = next(iter(app.state.store.rows.values()))
    assert event.source == "csv_upload"
    assert event.value_type == "monetary"
    assert event.value_currency == "USD"
    assert len(event.account_id_hash) == 64
    assert event.account_id_hash == event.user_id_hash


def test_reupload_replaces_the_period(client: TestClient) -> None:
    """The acceptance criterion: re-uploading a period is idempotent, never additive."""
    _upload(client)
    first_ids = set(app.state.store.rows)
    first_total = client.get("/v1/tenant/revenue-uploads", headers={"X-Tenant-Id": T}).json()[
        "snapshots"
    ][0]["total_amount_micro"]

    r = _upload(client)
    assert r.status_code == 200, r.text
    assert set(app.state.store.rows) == first_ids
    assert len(app.state.store.rows) == 2  # not 4
    snapshots = client.get("/v1/tenant/revenue-uploads", headers={"X-Tenant-Id": T}).json()[
        "snapshots"
    ]
    assert len(snapshots) == 1  # one manifest row per period, always
    assert snapshots[0]["total_amount_micro"] == first_total


def test_reupload_drops_an_account_that_left_the_file(client: TestClient) -> None:
    # The case a deterministic id alone cannot handle: an account in the first snapshot and absent
    # from the second must disappear, not linger at its old amount forever.
    _upload(client)
    smaller = "account_id,period,amount,currency\nacct_1001,2026-08,12500.00,USD\n"
    _upload(client, csv=smaller)
    assert len(app.state.store.rows) == 1


def test_reupload_of_one_period_leaves_other_periods_alone(client: TestClient) -> None:
    _upload(client, csv="account_id,period,amount,currency\nacct_a,2026-07,10,USD\n")
    _upload(client, csv="account_id,period,amount,currency\nacct_a,2026-08,20,USD\n")
    assert len(app.state.store.rows) == 2
    _upload(client, csv="account_id,period,amount,currency\nacct_a,2026-08,30,USD\n")
    assert len(app.state.store.rows) == 2


def test_malformed_row_is_rejected_with_a_line_number(client: TestClient) -> None:
    bad = (
        "account_id,period,amount,currency\n"
        "acct_a,2026-08,100,USD\n"
        "acct_b,2026-08,not-a-number,USD\n"
    )
    r = _upload(client, csv=bad)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["errors"][0]["line"] == 3
    # All-or-nothing: the good row above it is not written either.
    assert app.state.store.rows == {}


def test_empty_csv_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/revenue-uploads", json={"csv": "   "}, headers={"X-Tenant-Id": T}
    )
    assert r.status_code == 422


def test_missing_csv_field_is_rejected(client: TestClient) -> None:
    r = client.post("/v1/tenant/revenue-uploads", json={}, headers={"X-Tenant-Id": T})
    assert r.status_code == 422


def test_oversized_csv_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/tenant/revenue-uploads",
        json={"csv": "account_id,period,amount,currency\n" + "x" * (9 * 1024 * 1024)},
        headers={"X-Tenant-Id": T},
    )
    assert r.status_code == 413


def test_list_is_empty_before_any_upload(client: TestClient) -> None:
    r = client.get("/v1/tenant/revenue-uploads", headers={"X-Tenant-Id": T})
    assert r.status_code == 200
    assert r.json()["snapshots"] == []


def test_delete_removes_events_and_manifest_together(client: TestClient) -> None:
    _upload(client)
    r = client.delete("/v1/tenant/revenue-uploads/2026-08", headers={"X-Tenant-Id": T})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert app.state.store.rows == {}
    assert client.get("/v1/tenant/revenue-uploads", headers={"X-Tenant-Id": T}).json()[
        "snapshots"
    ] == []


def test_delete_rejects_a_period_that_is_not_a_month(client: TestClient) -> None:
    r = client.delete("/v1/tenant/revenue-uploads/August", headers={"X-Tenant-Id": T})
    assert r.status_code == 422


def test_clickhouse_failure_writes_no_manifest_row(client: TestClient) -> None:
    # A manifest row without its events would claim a freshness nothing backs.
    app.state.store.fail_next_write = True
    r = _upload(client)
    assert r.status_code == 503
    assert app.state.revenue_uploads.rows == {}


def test_manifest_failure_rolls_the_events_back(client: TestClient) -> None:
    # And events without a manifest row would be presented as current forever, so the period is
    # rolled back rather than left behind with no honest "as of".
    app.state.revenue_uploads.fail_next_write = True
    r = _upload(client)
    assert r.status_code == 503
    assert app.state.store.rows == {}


def test_narrowed_revenue_sources_produce_a_warning_note(client: TestClient) -> None:
    # Silently dropped revenue is the bug CTO-194 fixed. A successful upload that the tenant's own
    # source narrowing will discard has to say so.
    class Cfg:
        revenue_sources = ("stripe",)

    app.state.tenant_revenue_sources = FakeRevenueSourceStore(Cfg())
    note = _upload(client).json()["note"]
    assert note is not None and "csv_upload" in note


def test_no_note_when_the_source_is_allowed(client: TestClient) -> None:
    class Cfg:
        revenue_sources = ("stripe", "csv_upload")

    app.state.tenant_revenue_sources = FakeRevenueSourceStore(Cfg())
    assert _upload(client).json()["note"] is None


def test_template_names_the_required_columns(client: TestClient) -> None:
    r = client.get("/v1/tenant/revenue-uploads/template")
    assert r.status_code == 200
    assert r.text.splitlines()[0] == "account_id,period,amount,currency"


def test_tenant_lookup_failure_is_a_404_not_a_503(client: TestClient) -> None:
    class Missing(FakeUploadStore):
        def list(self, tenant_id: str):
            raise RevenueUploadError("no tenant named 'ghost'")

    app.state.revenue_uploads = Missing()
    r = client.get("/v1/tenant/revenue-uploads", headers={"X-Tenant-Id": "ghost"})
    assert r.status_code == 404
