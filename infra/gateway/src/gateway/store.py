"""ClickHouse writer. Wraps clickhouse-connect with span/event/identity inserts."""

from __future__ import annotations

from datetime import datetime, timezone

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tally.wire import BusinessEvent, IdentityLink

from gateway.config import Settings
from gateway.mapping import COLUMNS

_BUSINESS_EVENT_COLS = (
    "TenantId", "BusinessEventId", "EventName", "UserIdHash", "OccurredAt", "IngestedAt",
    "ValueAmountMicro", "ValueCurrency", "ValueType", "Source", "RawPayload",
)

_IDENTITY_COLS = (
    "TenantId", "IdentityA", "IdentityAType", "IdentityB", "IdentityBType",
    "UserIdHashKeyVersion", "Confidence", "ObservedAt", "Source",
)


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


class ClickHouseStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = clickhouse_connect.get_client(
                host=self._settings.clickhouse_host,
                port=self._settings.clickhouse_port,
                username=self._settings.clickhouse_user,
                password=self._settings.clickhouse_password,
                database=self._settings.clickhouse_db,
            )
        return self._client

    def ping(self) -> bool:
        return self.client.query("SELECT 1").result_rows[0][0] == 1

    def insert_spans(self, rows: list[tuple[object, ...]]) -> int:
        if not rows:
            return 0
        self.client.insert("otel_spans", rows, column_names=list(COLUMNS))
        return len(rows)

    def insert_business_events(self, tenant_id: str, events: list[BusinessEvent]) -> int:
        if not events:
            return 0
        now = datetime.now(tz=timezone.utc)
        rows = [
            (
                tenant_id,
                e.business_event_id,
                e.event_name,
                e.user_id_hash[:64],
                _ts(e.occurred_at_ns),
                now,
                e.value_amount_micro,
                e.value_currency,
                e.value_type,
                e.source,
                "",
            )
            for e in events
        ]
        self.client.insert("business_events", rows, column_names=list(_BUSINESS_EVENT_COLS))
        return len(rows)

    def insert_identity_links(self, tenant_id: str, links: list[IdentityLink]) -> int:
        if not links:
            return 0
        rows = [
            (
                tenant_id,
                ln.identity_a[:64],
                ln.identity_a_type,
                ln.identity_b[:64],
                ln.identity_b_type,
                "",
                ln.confidence,
                _ts(ln.observed_at_ns),
                ln.source,
            )
            for ln in links
        ]
        self.client.insert("identity_graph", rows, column_names=list(_IDENTITY_COLS))
        return len(rows)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
