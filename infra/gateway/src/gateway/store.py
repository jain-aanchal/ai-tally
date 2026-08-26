"""ClickHouse writer. Wraps clickhouse-connect with span/event/identity inserts."""

from __future__ import annotations

from datetime import datetime, timezone

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tally.wire import BusinessEvent, IdentityLink

from gateway.config import Settings
from gateway.mapping import COLUMNS

_BUSINESS_EVENT_COLS = (
    "TenantId", "BusinessEventId", "EventName", "UserIdHash", "AccountIdHash", "OccurredAt",
    "IngestedAt", "ValueAmountMicro", "ValueCurrency", "ValueType", "Source", "RawPayload",
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

    def span_exists(self, tenant_id: str, span_id: str) -> bool:
        """Return True if a span with this deterministic SpanId already exists for the tenant.

        The idempotency guard for connector-emitted synthetic spans (CTO-143): ``otel_spans`` is a
        plain MergeTree with no dedup, so the compute/egress connectors check here before inserting
        a day's span. Tenant-scoped so the (bloom-filtered) SpanId lookup stays cheap and can't cross
        tenants.
        """
        result = self.client.query(
            "SELECT count() FROM otel_spans WHERE TenantId = %(t)s AND SpanId = %(s)s",
            parameters={"t": tenant_id, "s": span_id},
        )
        return result.result_rows[0][0] > 0

    def business_event_exists(self, tenant_id: str, business_event_id: str) -> bool:
        """Return True if this tenant already has a business event with this id.

        The durable half of the generic revenue API's idempotency (CTO-199). ``business_events`` is
        a ``ReplacingMergeTree`` ordered by ``(TenantId, BusinessEventId)``, so a re-posted event
        collapses onto the original *eventually*, but the attribution revenue sum reads the table
        without ``FINAL``, so between the second insert and the next merge the money would be
        counted twice. Probing first is what makes a retry safe now rather than at merge time.

        Cheap: the id is the table's own sort key, so this is a primary-index lookup.
        """
        result = self.client.query(
            "SELECT count() FROM business_events "
            "WHERE TenantId = %(t)s AND BusinessEventId = %(b)s",
            parameters={"t": tenant_id, "b": business_event_id},
        )
        return result.result_rows[0][0] > 0

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
                # CTO-195: the account the revenue belongs to. '' when the provider named none,
                # which the attribution surfaces read as unattributed rather than as a customer.
                e.account_id_hash[:64],
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

    def delete_business_events_by_id_prefix(
        self, tenant_id: str, source: str, id_prefix: str
    ) -> None:
        """Synchronously drop every business event of one source whose id starts with a prefix.

        The delete half of the CSV revenue upload's replace-a-period (CTO-198). ``business_events``
        is a ``ReplacingMergeTree`` on ``(TenantId, BusinessEventId)``, which collapses a re-upload
        of the SAME account onto one row but does nothing about an account that was in the previous
        upload and is absent from this one — that row would linger and inflate revenue forever.
        Deleting the whole period first is what makes an upload a true snapshot replacement.

        Scoped by ``Source`` as well as by the derived-id prefix so this can never reach a row a
        real connector wrote, whatever a caller passes as the prefix.

        ``lightweight_deletes_sync = 2`` waits for the delete to be visible on every replica before
        returning, so the INSERT that follows can never race it and momentarily double the total.
        """
        self.client.command(
            """
            DELETE FROM business_events
            WHERE TenantId = %(tenant)s AND Source = %(source)s
              AND startsWith(BusinessEventId, %(prefix)s)
            """,
            parameters={"tenant": tenant_id, "source": source, "prefix": id_prefix},
            settings={"lightweight_deletes_sync": 2},
        )

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
