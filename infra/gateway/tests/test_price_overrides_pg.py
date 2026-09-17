# SPDX-License-Identifier: Apache-2.0
"""The price override ledger against a REAL Postgres (CTO-416).

Separate from ``test_app_price_overrides.py``, which swaps a fake store in, because a fake cannot
assert the things this feature's guarantees actually rest on:

* the version really is assigned by the INSERT (``MAX(version) + 1`` over the slot), so two replicas
  appending at once cannot both write version N;
* ``uq_price_overrides_slot_version`` really is what decides that race, and psycopg's
  ``UniqueViolation`` really is mapped to the 409 the endpoint promises;
* the append-only TRIGGER really refuses an UPDATE, which is the guarantee that makes a past invoice
  explainable;
* migration 0035 really did create both of those objects. It is one transaction precisely so that a
  half-applied run cannot leave a database that looks migrated and enforces neither, and nothing
  else in the suite would notice if it did (CTO-416 review).

Every tenant these create is named ``price-override-test-*`` and is removed in teardown, so the file
is safe to point at a stack that also holds a demo corpus. Skipped unless ``TALLY_TEST_POSTGRES_DSN``
is set, so a checkout with no infrastructure still runs the suite green:

    TALLY_TEST_POSTGRES_DSN=postgresql://tally:tally@localhost:5432/tally \\
      uv run --extra dev pytest -q tests/test_price_overrides_pg.py
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from gateway.config import Settings
from gateway.price_overrides import (
    PriceOverrideConflict,
    PriceOverrideStore,
    TenantNotFound,
)
from tally.pricing import PriceType, Unit

DSN = os.environ.get("TALLY_TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TALLY_TEST_POSTGRES_DSN to a migrated control-plane database"
)

NAME_PREFIX = "price-override-test-"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DSN, autocommit=True) as connection:
        yield connection
        with connection.cursor() as cur:
            # Targets the name prefix only, never a UUID, so a typo cannot reach the corpus. The
            # override rows go with the tenant through ON DELETE CASCADE, which is also the one
            # delete the append-only trigger deliberately does not block.
            cur.execute("DELETE FROM tenants WHERE name LIKE %s", (NAME_PREFIX + "%",))


@pytest.fixture
def tenant(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants (name, region, plan, hash_salt_kek_ref)
            VALUES (%s, 'local', 'free', 'arn:aws:kms:us-east-1:0:key/test') RETURNING id
            """,
            (f"{NAME_PREFIX}{uuid.uuid4().hex[:8]}",),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def store() -> PriceOverrideStore:
    return PriceOverrideStore(Settings(postgres_dsn=DSN))


def _append(store: PriceOverrideStore, tenant: str, price: str | None, **over: object):
    return store.append(
        tenant,
        provider="openai",
        model="gpt-4o-mini",
        price_type=PriceType.INPUT,
        price_per_unit=None if price is None else Decimal(price),
        actor="amy@example.com",
        reason="CTO-416 contract",
        unit=Unit.PER_MILLION_TOKENS,
        **over,  # type: ignore[arg-type]
    )


def test_the_version_chain_is_assigned_by_the_insert(
    store: PriceOverrideStore, tenant: str
) -> None:
    """Not by an in-process counter: production runs more than one replica."""
    first = _append(store, tenant, "0.015")
    second = _append(store, tenant, "0.010")
    tomb = _append(store, tenant, None)

    assert (first.version, first.supersedes) == (1, None)
    assert (second.version, second.supersedes) == (2, 1)
    assert (tomb.version, tomb.supersedes) == (3, 2)
    assert tomb.revoked and tomb.price_per_unit is None


def test_a_rate_survives_the_round_trip_as_decimal(
    store: PriceOverrideStore, tenant: str
) -> None:
    """Money never becomes a float, NUMERIC(20,8) to Decimal and back."""
    _append(store, tenant, "0.00012345")
    loaded = [r for r in store.load_all() if r.tenant_id == tenant]
    assert len(loaded) == 1
    assert isinstance(loaded[0].price_per_unit, Decimal)
    assert loaded[0].price_per_unit == Decimal("0.00012345")


def test_a_concurrent_append_of_the_same_version_is_a_conflict_not_a_second_row(
    store: PriceOverrideStore, tenant: str, conn: psycopg.Connection
) -> None:
    """The unique index is what decides a concurrent append; the store maps it to the 409.

    Genuinely concurrent, because the interesting case cannot be staged with a committed row: two
    replicas both read MAX(version) = 1 and both try to write version 2. One commits, the other
    blocks on the index and then fails, and the loser must be told to re-read rather than have its
    rate silently land as a different version or, worse, alongside the winner's.
    """
    _append(store, tenant, "0.015")  # version 1

    inserted = threading.Event()

    def other_replica() -> None:
        with psycopg.connect(DSN) as rival, rival.cursor() as cur:
            cur.execute(
                """
                INSERT INTO price_catalog_overrides
                    (tenant_id, provider, model, price_type, unit, price_per_unit, valid_from,
                     version, supersedes, actor, reason)
                VALUES (%s, 'openai', 'gpt-4o-mini', 'input', 'per_million_tokens', 0.012, now(),
                        2, 1, 'other-replica', 'concurrent append')
                """,
                (tenant,),
            )
            inserted.set()  # written, NOT yet committed: this replica is mid-transaction
            time.sleep(0.5)
            rival.commit()

    rival_thread = threading.Thread(target=other_replica)
    rival_thread.start()
    try:
        assert inserted.wait(timeout=5)
        # Computes MAX(version) = 1 from committed data, tries version 2, blocks on the index, and
        # loses when the other transaction commits.
        with pytest.raises(PriceOverrideConflict):
            _append(store, tenant, "0.011")
    finally:
        rival_thread.join(timeout=10)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM price_catalog_overrides WHERE tenant_id = %s AND version = 2",
            (tenant,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT actor FROM price_catalog_overrides WHERE tenant_id = %s AND version = 2",
            (tenant,),
        )
        assert cur.fetchone()[0] == "other-replica"  # the winner's row, intact


def test_an_update_is_refused_by_the_database(
    store: PriceOverrideStore, tenant: str, conn: psycopg.Connection
) -> None:
    """Append-only is enforced by migration 0035, not merely by this module having no update path."""
    _append(store, tenant, "0.015")
    with pytest.raises(psycopg.errors.RaiseException), conn.cursor() as cur:
        cur.execute(
            "UPDATE price_catalog_overrides SET price_per_unit = 9.99 WHERE tenant_id = %s",
            (tenant,),
        )


def test_migration_0035_created_the_objects_the_guarantees_live_in(
    conn: psycopg.Connection,
) -> None:
    """Both objects, or the audit guarantee and the race guard silently do not exist.

    The first version of the migration had no transaction, so a run that aborted on the unique index
    left the columns added and NEITHER object created, on a database that then looked migrated.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'price_catalog_overrides' "
            "AND indexname = 'uq_price_overrides_slot_version'"
        )
        assert cur.fetchone() is not None, "the concurrent-append guard is missing"
        cur.execute(
            "SELECT 1 FROM pg_trigger WHERE tgrelid = 'price_catalog_overrides'::regclass "
            "AND tgname = 'trg_price_overrides_append_only' AND NOT tgisinternal"
        )
        assert cur.fetchone() is not None, "the append-only guard is missing"
        # A tombstone needs a nullable price; the 0001 column was NOT NULL.
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'price_catalog_overrides' AND column_name = 'price_per_unit'"
        )
        assert cur.fetchone()[0] == "YES"


def test_a_well_formed_uuid_for_no_tenant_is_not_found_rather_than_a_500(
    store: PriceOverrideStore,
) -> None:
    """The UUID path used to reach the INSERT and come back as a ForeignKeyViolation, i.e. a 500.

    A name that does not exist already answered not-found, and the two spellings should not disagree
    about what a wrong tenant is (CTO-416 review).
    """
    absent = str(uuid.uuid4())
    with pytest.raises(TenantNotFound):
        _append(store, absent, "0.015")
    with pytest.raises(TenantNotFound):
        store.history(absent)


def test_history_and_load_all_come_back_in_ledger_order(
    store: PriceOverrideStore, tenant: str
) -> None:
    """Out of order, a tombstone could be overtaken by the rate it withdrew."""
    _append(store, tenant, "0.015", valid_from=date(2026, 1, 1))
    _append(store, tenant, "0.010", valid_from=date(2026, 6, 1))
    _append(store, tenant, None)

    assert [r.version for r in store.history(tenant)] == [1, 2, 3]
    assert [r.version for r in store.load_all() if r.tenant_id == tenant] == [1, 2, 3]


def test_a_tenant_name_resolves_the_same_as_its_uuid(
    store: PriceOverrideStore, tenant: str, conn: psycopg.Connection
) -> None:
    """The name-vs-UUID trap: the dashboard sends one spelling and the SDK another."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tenants WHERE id = %s", (tenant,))
        name = cur.fetchone()[0]

    _append(store, tenant, "0.015")
    by_name = store.history(name)
    assert [r.version for r in by_name] == [1]
    assert by_name[0].tenant_id == tenant  # stored under the UUID whichever spelling was used


def test_a_tenants_own_spelling_is_usable_but_a_uuid_shaped_name_is_not(
    store: PriceOverrideStore, conn: psycopg.Connection
) -> None:
    """The cross-tenant leak, end to end against the real table (CTO-416 review).

    A tenant named after ANOTHER tenant's canonical id must not have its override registered under
    the victim's catalog key. ``tenants.name`` is free text from the Clerk webhook, so this is a
    thing a customer can do to themselves, on purpose, through a supported path.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants (name, region, plan, hash_salt_kek_ref)
            VALUES (%s, 'local', 'free', 'arn:aws:kms:us-east-1:0:key/test') RETURNING id
            """,
            (f"{NAME_PREFIX}victim-{uuid.uuid4().hex[:8]}",),
        )
        victim = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO tenants (name, region, plan, hash_salt_kek_ref)
            VALUES (%s, 'local', 'free', 'arn:aws:kms:us-east-1:0:key/test') RETURNING id
            """,
            (victim,),  # the attacker names their org after the victim's UUID
        )
        attacker = str(cur.fetchone()[0])

    spellings = store.load_tenant_spellings()
    assert victim not in spellings.usable.get(attacker, [])
    assert victim in spellings.ambiguous


MIGRATION = (
    Path(__file__).resolve().parents[3] / "db" / "postgres" / "0035_price_override_ledger.sql"
)


def test_migration_0035_is_replayable_against_a_populated_table(
    conn: psycopg.Connection, store: PriceOverrideStore, tenant: str
) -> None:
    """Replaying the file must succeed and change nothing (CTO-416 round 2 nit).

    Every other migration here is safe to replay, and this one has to be too, because a running
    stack applies migrations by hand and an operator re-running the file is normal. It is also the
    property that keeps the pre-backfill DROP TRIGGER honest: the backfill is an UPDATE, the trigger
    it creates refuses UPDATEs, and without the drop a replay fails. The backfill is deliberately
    unconditional within its scope so that this test exercises exactly that.

    Runs against a slot with pre-ledger rows (what the backfill is for) AND a ledger-managed slot
    (which it must leave alone).
    """
    sql = MIGRATION.read_text()

    # A pre-ledger slot: rows as the 0001 table would have held them, several windows, actor left at
    # the migration's own backfill marker.
    with conn.cursor() as cur:
        for version, (start, rate) in enumerate(
            ((date(2026, 1, 1), "0.05"), (date(2026, 6, 1), "0.04")), start=1
        ):
            cur.execute(
                """
                INSERT INTO price_catalog_overrides
                    (tenant_id, provider, model, price_type, unit, price_per_unit, valid_from,
                     version, supersedes, actor, reason)
                VALUES (%s, 'openai', 'legacy-model', 'input', 'per_million_tokens', %s, %s,
                        %s, %s, 'pre-ledger', 'pre-ledger row, provenance unknown')
                """,
                (tenant, rate, start, version, version - 1 or None),
            )

    # A ledger-managed slot, including a BACKDATED append: renumbering this one would rewrite a
    # supersedes chain and could collide with the unique index.
    _append(store, tenant, "0.015", valid_from=date(2026, 9, 1))
    _append(store, tenant, "0.012", valid_from=date(2026, 2, 1))

    def snapshot() -> list[tuple]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT model, version, supersedes, valid_from, price_per_unit, actor "
                "FROM price_catalog_overrides WHERE tenant_id = %s ORDER BY model, version",
                (tenant,),
            )
            return cur.fetchall()

    before = snapshot()
    with psycopg.connect(DSN, autocommit=True) as replay, replay.cursor() as cur:
        cur.execute(sql)  # must not raise
    assert snapshot() == before

    # And the objects the guarantees live in are still there afterwards.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_trigger WHERE tgrelid = 'price_catalog_overrides'::regclass "
            "AND tgname = 'trg_price_overrides_append_only' AND NOT tgisinternal"
        )
        assert cur.fetchone() is not None
