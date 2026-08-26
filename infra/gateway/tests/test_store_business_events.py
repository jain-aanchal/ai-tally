"""The business_events insert contract: columns, ordering, and AccountIdHash (CTO-195).

``insert_business_events`` builds positional row tuples against a hand-maintained column list. A
column added to one and not the other silently shifts every value after it, which would write an
account hash into ``OccurredAt``. These tests pin the two together and pin both against the
canonical DDL, with a fake client so no ClickHouse is needed.
"""

from __future__ import annotations

from pathlib import Path

from tally.wire import BusinessEvent

from gateway.store import _BUSINESS_EVENT_COLS, ClickHouseStore

DDL = Path(__file__).resolve().parents[3] / "db" / "clickhouse" / "attribution.sql"


class FakeClient:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list, list[str]]] = []

    def insert(self, table: str, rows: list, column_names: list[str]) -> None:
        self.inserts.append((table, rows, column_names))


def _store_with_fake() -> tuple[ClickHouseStore, FakeClient]:
    store = ClickHouseStore.__new__(ClickHouseStore)
    client = FakeClient()
    store._client = client  # type: ignore[attr-defined]
    store._settings = None  # type: ignore[attr-defined]
    return store, client


def _event(**kw: object) -> BusinessEvent:
    base: dict[str, object] = {
        "business_event_id": "evt-1",
        "event_name": "conversion",
        "user_id_hash": "u" * 64,
        "occurred_at_ns": 1_777_000_000_000_000_000,
        "value_amount_micro": 4_900_000,
        "source": "stripe",
    }
    base.update(kw)
    return BusinessEvent(**base)  # type: ignore[arg-type]


def test_row_width_matches_the_column_list() -> None:
    store, client = _store_with_fake()
    store.insert_business_events("t-acme", [_event()])
    _table, rows, columns = client.inserts[0]
    assert len(rows[0]) == len(columns) == len(_BUSINESS_EVENT_COLS)


def test_account_id_hash_is_written_in_its_declared_position() -> None:
    store, client = _store_with_fake()
    account = "a" * 64
    store.insert_business_events("t-acme", [_event(account_id_hash=account)])
    _table, rows, columns = client.inserts[0]
    assert rows[0][columns.index("AccountIdHash")] == account
    # And it did not displace the user hash, which is the whole backward-compatibility claim.
    assert rows[0][columns.index("UserIdHash")] == "u" * 64


def test_an_event_with_no_account_writes_the_unattributed_default() -> None:
    store, client = _store_with_fake()
    store.insert_business_events("t-acme", [_event()])
    _table, rows, columns = client.inserts[0]
    assert rows[0][columns.index("AccountIdHash")] == ""


def test_over_long_account_hash_is_truncated_to_the_fixedstring_width() -> None:
    store, client = _store_with_fake()
    store.insert_business_events("t-acme", [_event(account_id_hash="z" * 200)])
    _table, rows, columns = client.inserts[0]
    assert rows[0][columns.index("AccountIdHash")] == "z" * 64


def test_every_inserted_column_exists_in_the_canonical_ddl() -> None:
    ddl = DDL.read_text()
    table = ddl.split("CREATE TABLE IF NOT EXISTS business_events", 1)[1].split("ENGINE", 1)[0]
    for column in _BUSINESS_EVENT_COLS:
        assert column in table, f"{column} is inserted but absent from business_events DDL"
