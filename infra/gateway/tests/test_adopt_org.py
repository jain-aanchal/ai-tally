# SPDX-License-Identifier: Apache-2.0
"""Clerk org adoption (CTO-364), against an in-memory fake of the statements the command runs.

The decision logic lives here: which cases refuse, which are idempotent, what the dry run does and
does not write, and that a mid-operation failure leaves nothing behind. The claims that are
properties of Postgres rather than of this Python (the cascade, the partial unique index, the
transaction boundary) are asserted in ``test_adopt_org_pg.py`` against a real database, because a
dict cannot prove them.
"""

from __future__ import annotations

import datetime as _dt
import uuid

import pytest

from gateway import adopt_org
from gateway.adopt_org import (
    DISPOSABLE_TABLES,
    AdoptError,
    OrgAdopter,
    render,
)
from gateway.config import Settings

NOW = _dt.datetime(2026, 9, 9, 22, 40, tzinfo=_dt.timezone.utc)

#: Stands in for the catalog read. The real list comes from pg_constraint; the fake only needs
#: enough tables to exercise the "cascading" and "would orphan" branches.
CASCADING = ["api_keys", "usage_limits", "value_events"]
UNLINKED = ["ingest_batch_idempotency", "scheduler_runs"]


class SpyKeyProvider:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self._fail = fail

    def mint(self) -> str:  # pragma: no cover - adoption never mints
        raise AssertionError("adopt_org must never mint key material")

    def delete(self, ref: str) -> None:
        if self._fail:
            raise RuntimeError("secrets manager throttled")
        self.deleted.append(ref)


class _Tenant:
    def __init__(self, name: str, org: str | None, ref: str) -> None:
        self.id = str(uuid.uuid4())
        self.name = name
        self.clerk_org_id = org
        self.plan = "free"
        self.hash_salt_kek_ref = ref
        self.created_at = NOW

    def as_row(self) -> tuple:
        return (
            self.id,
            self.name,
            self.clerk_org_id,
            self.plan,
            self.hash_salt_kek_ref,
            self.created_at,
        )


class _FakeDB:
    """The mutable world the fake connection reads and writes."""

    def __init__(self) -> None:
        self.tenants: dict[str, _Tenant] = {}
        #: table -> tenant_id -> row count
        self.rows: dict[str, dict[str, int]] = {}
        self.committed = False
        self.rolled_back = False
        #: set to raise on the statement whose prefix matches, to test mid-operation failure
        self.fail_on: str = ""

    def add(self, tenant: _Tenant) -> _Tenant:
        self.tenants[tenant.id] = tenant
        return tenant

    def count(self, table: str, tenant_id: str) -> int:
        return self.rows.get(table, {}).get(tenant_id, 0)


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._result: list[tuple] = []
        self.rowcount = -1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        s = " ".join(sql.split())
        if self._db.fail_on and s.startswith(self._db.fail_on):
            raise RuntimeError("injected failure")
        self._result = []
        self.rowcount = -1
        if s.startswith("LOCK TABLE tenants"):
            return
        if s.startswith("SELECT id, name, clerk_org_id") and "WHERE id =" in s:
            found = self._db.tenants.get(str(params[0]))
            self._result = [found.as_row()] if found else []
            return
        if s.startswith("SELECT id, name, clerk_org_id") and "WHERE clerk_org_id =" in s:
            self._result = [
                t.as_row() for t in self._db.tenants.values() if t.clerk_org_id == params[0]
            ]
            return
        if "FROM pg_constraint" in s and "confdeltype = 'c'" in s:
            self._result = [(t,) for t in CASCADING]
            return
        if "FROM information_schema.columns" in s:
            self._result = [(t,) for t in UNLINKED]
            return
        if s.startswith("SELECT count(*) FROM"):
            table = s.split('"')[1]
            self._result = [(self._db.count(table, str(params[0])),)]
            return
        if s.startswith("DELETE FROM tenants"):
            removed = self._db.tenants.pop(str(params[0]), None)
            self.rowcount = 1 if removed else 0
            return
        if s.startswith("UPDATE tenants SET clerk_org_id"):
            found = self._db.tenants.get(str(params[1]))
            if found is None:
                self.rowcount = 0
                return
            found.clerk_org_id = params[0]
            self.rowcount = 1
            return
        raise AssertionError(f"fake cursor saw an unexpected statement: {s}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return self._result


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        self._db.committed = True

    def rollback(self) -> None:
        self._db.rolled_back = True

    def close(self) -> None:
        pass


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    world = _FakeDB()
    monkeypatch.setattr(adopt_org.psycopg, "connect", lambda _dsn: _FakeConn(world))
    return world


def _adopter(keys: SpyKeyProvider | None = None, spans: int = 0) -> OrgAdopter:
    return OrgAdopter(
        Settings(postgres_dsn="postgresql://fake/fake"),
        key_provider=keys or SpyKeyProvider(),
        span_counter=lambda _tid: spans,
    )


def _corpus(db: _FakeDB) -> _Tenant:
    tenant = db.add(_Tenant("Nova corpus", None, "local://hmac/corpus/v1"))
    db.rows.setdefault("value_events", {})[tenant.id] = 4200
    db.rows.setdefault("api_keys", {})[tenant.id] = 1
    return tenant


def _webhook_tenant(db: _FakeDB, org: str) -> _Tenant:
    """What provisioning creates: a tenant row, one usage_limits row, and nothing else."""
    tenant = db.add(_Tenant("Nova", org, "local://hmac/fresh/v1"))
    db.rows.setdefault("usage_limits", {})[tenant.id] = 1
    return tenant


# -- the happy path ------------------------------------------------------------------------------


def test_dry_run_writes_nothing(db: _FakeDB) -> None:
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")

    report = _adopter().run(clerk_org_id="org_nova", target_tenant_id=target.id)

    assert report.applied is False
    assert db.committed is False
    assert db.rolled_back is True
    assert incumbent.id in db.tenants, "dry run must not delete the incumbent"
    assert target.clerk_org_id is None, "dry run must not move the org"
    assert report.incumbent is not None and report.incumbent.id == incumbent.id
    assert report.tables_examined == len(CASCADING) + len(UNLINKED)
    assert "DRY RUN" in render(report)


def test_confirm_deletes_the_incumbent_and_moves_the_org(db: _FakeDB) -> None:
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")
    keys = SpyKeyProvider()

    report = _adopter(keys).run(
        clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True
    )

    assert report.applied is True
    assert db.committed is True
    assert incumbent.id not in db.tenants
    assert db.tenants[target.id].clerk_org_id == "org_nova"
    assert report.target_after is not None
    # The corpus keeps its OWN key: every account hash in it was computed under this reference, and
    # replacing it would make the corpus unjoinable to itself.
    assert report.target_after.hash_salt_kek_ref == "local://hmac/corpus/v1"
    assert keys.deleted == ["local://hmac/fresh/v1"], "the discarded tenant's key must be released"
    assert "ADOPTED, committed" in render(report)


def test_unclaimed_org_is_a_plain_update(db: _FakeDB) -> None:
    """Local, or production before the webhook lands: nothing to dispose of."""
    target = _corpus(db)
    keys = SpyKeyProvider()

    report = _adopter(keys).run(
        clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True
    )

    assert report.incumbent is None
    assert db.tenants[target.id].clerk_org_id == "org_nova"
    assert keys.deleted == [], "no incumbent means no key to delete"


def test_usage_limits_alone_does_not_block(db: _FakeDB) -> None:
    """A freshly provisioned tenant has exactly one usage_limits row. That is expected, not data."""
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")

    report = _adopter().run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert report.applied is True
    assert [c.table for c in report.census] == ["usage_limits"]
    assert "usage_limits" in DISPOSABLE_TABLES
    assert incumbent.id not in db.tenants


# -- idempotence ---------------------------------------------------------------------------------


def test_already_adopted_is_a_no_op(db: _FakeDB) -> None:
    target = _corpus(db)
    target.clerk_org_id = "org_nova"
    keys = SpyKeyProvider()

    report = _adopter(keys).run(
        clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True
    )

    assert report.already_adopted is True
    assert report.applied is False
    assert db.committed is False
    assert keys.deleted == []
    assert "ALREADY ADOPTED" in render(report)


def test_running_twice_is_safe(db: _FakeDB) -> None:
    target = _corpus(db)
    _webhook_tenant(db, "org_nova")
    adopter = _adopter()

    first = adopter.run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)
    second = adopter.run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert first.applied is True
    assert second.already_adopted is True and second.applied is False
    assert db.tenants[target.id].clerk_org_id == "org_nova"
    assert len(db.tenants) == 1


# -- refusals ------------------------------------------------------------------------------------


def test_missing_target_refuses(db: _FakeDB) -> None:
    with pytest.raises(AdoptError, match="no tenant"):
        _adopter().run(
            clerk_org_id="org_nova", target_tenant_id=str(uuid.uuid4()), confirm=True
        )
    assert db.committed is False


def test_a_name_is_not_accepted_as_a_target(db: _FakeDB) -> None:
    """CLAUDE.md: never feed a name into a UUID column. Here it is also the deletion hazard."""
    with pytest.raises(AdoptError, match="must be the tenant UUID"):
        _adopter().run(clerk_org_id="org_nova", target_tenant_id="local-dev", confirm=True)


def test_empty_org_refuses(db: _FakeDB) -> None:
    target = _corpus(db)
    with pytest.raises(AdoptError, match="non-empty"):
        _adopter().run(clerk_org_id="   ", target_tenant_id=target.id, confirm=True)


def test_target_with_a_different_org_refuses(db: _FakeDB) -> None:
    target = _corpus(db)
    target.clerk_org_id = "org_other"
    _webhook_tenant(db, "org_nova")

    with pytest.raises(AdoptError, match="--allow-reassign"):
        _adopter().run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert db.tenants[target.id].clerk_org_id == "org_other"
    assert db.committed is False


def test_allow_reassign_permits_it_and_says_so(db: _FakeDB) -> None:
    target = _corpus(db)
    target.clerk_org_id = "org_other"
    _webhook_tenant(db, "org_nova")

    report = _adopter().run(
        clerk_org_id="org_nova",
        target_tenant_id=target.id,
        confirm=True,
        allow_reassign=True,
    )

    assert report.applied is True
    assert db.tenants[target.id].clerk_org_id == "org_nova"
    assert any("org_other" in note for note in report.notes)


def test_non_empty_incumbent_refuses_and_names_the_tables(db: _FakeDB) -> None:
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")
    db.rows.setdefault("value_events", {})[incumbent.id] = 17

    with pytest.raises(AdoptError, match="value_events=17"):
        _adopter().run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert incumbent.id in db.tenants
    assert db.committed is False


def test_rows_in_an_unlinked_table_refuse_too(db: _FakeDB) -> None:
    """These have no FK, so a DELETE would orphan them rather than cascade them away."""
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")
    db.rows.setdefault("scheduler_runs", {})[incumbent.id] = 3

    with pytest.raises(AdoptError, match="scheduler_runs=3"):
        _adopter().run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert incumbent.id in db.tenants


def test_incumbent_with_spans_refuses(db: _FakeDB) -> None:
    target = _corpus(db)
    incumbent = _webhook_tenant(db, "org_nova")

    with pytest.raises(AdoptError, match="512,081 spans"):
        _adopter(spans=512081).run(
            clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True
        )

    assert incumbent.id in db.tenants


def test_unreadable_span_count_refuses_rather_than_assuming_zero(db: _FakeDB) -> None:
    """Honest under uncertainty: unknown is not zero, and only zero makes a tenant disposable."""
    target = _corpus(db)
    _webhook_tenant(db, "org_nova")

    def unreachable(_tid: str) -> int:
        raise ConnectionError("clickhouse unreachable")

    adopter = OrgAdopter(
        Settings(postgres_dsn="postgresql://fake/fake"),
        key_provider=SpyKeyProvider(),
        span_counter=unreachable,
    )
    with pytest.raises(AdoptError, match="not a zero count"):
        adopter.run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)
    assert db.committed is False


def test_skip_telemetry_check_proceeds_and_records_that_it_skipped(db: _FakeDB) -> None:
    target = _corpus(db)
    _webhook_tenant(db, "org_nova")

    def unreachable(_tid: str) -> int:  # pragma: no cover - must never be called
        raise AssertionError("the span count must not be attempted when skipped")

    adopter = OrgAdopter(
        Settings(postgres_dsn="postgresql://fake/fake"),
        key_provider=SpyKeyProvider(),
        span_counter=unreachable,
    )
    report = adopter.run(
        clerk_org_id="org_nova",
        target_tenant_id=target.id,
        confirm=True,
        check_telemetry=False,
    )
    assert report.applied is True
    assert report.span_count is None
    assert "not checked" in render(report)


# -- failure in the middle -----------------------------------------------------------------------


def test_failure_between_the_delete_and_the_update_rolls_back(db: _FakeDB) -> None:
    """The whole reason this is one transaction: an org pointing nowhere is the bad outcome."""
    target = _corpus(db)
    # The incumbent is created for its side effect: it is what the DELETE half of the transaction
    # removes. Nothing here needs to name it, matching the sibling test below.
    _webhook_tenant(db, "org_nova")
    keys = SpyKeyProvider()
    db.fail_on = "UPDATE tenants SET clerk_org_id"

    with pytest.raises(RuntimeError, match="injected failure"):
        _adopter(keys).run(clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True)

    assert db.committed is False
    assert db.rolled_back is True
    # A real Postgres rolls the DELETE back with the transaction; the fake mutates eagerly, so what
    # matters here is that nothing was committed and no key material was destroyed.
    assert keys.deleted == [], "a rolled-back adoption must not delete the incumbent's key"


def test_key_delete_failure_does_not_undo_a_committed_adoption(db: _FakeDB) -> None:
    """The adoption is done. Report the orphan rather than turn a success into an exception."""
    target = _corpus(db)
    _webhook_tenant(db, "org_nova")

    report = _adopter(SpyKeyProvider(fail=True)).run(
        clerk_org_id="org_nova", target_tenant_id=target.id, confirm=True
    )

    assert report.applied is True
    assert db.tenants[target.id].clerk_org_id == "org_nova"
    assert report.key_ref_orphaned == "local://hmac/fresh/v1"
    assert "delete it by hand" in render(report)


# -- the CLI shell -------------------------------------------------------------------------------


def test_main_returns_1_and_prints_the_refusal(
    db: _FakeDB, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(adopt_org, "OrgAdopter", lambda _s: _adopter())
    code = adopt_org.main(["--clerk-org", "org_nova", "--tenant", str(uuid.uuid4())])
    assert code == 1
    assert "REFUSED" in capsys.readouterr().err


def test_main_defaults_to_a_dry_run(
    db: _FakeDB, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _corpus(db)
    _webhook_tenant(db, "org_nova")
    monkeypatch.setattr(adopt_org, "OrgAdopter", lambda _s: _adopter())

    code = adopt_org.main(["--clerk-org", "org_nova", "--tenant", target.id])

    assert code == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert db.committed is False
    assert target.clerk_org_id is None
