# SPDX-License-Identifier: Apache-2.0
"""Clerk org adoption against a REAL Postgres (CTO-364).

Separate from ``test_adopt_org.py`` because these assert the three things a fake cannot:

* the DELETE really does cascade, so no child row survives its tenant;
* ``uq_tenants_clerk_org_id`` really is what makes the hand-written UPDATE fail, and the command
  really does get past it;
* the DELETE and the UPDATE really are one transaction, so an injected failure between them leaves
  the incumbent alive and the org exactly where it was.

Every tenant these create is named ``adopt-org-test-*`` and is removed in teardown, so the file is
safe to point at a stack that also holds a demo corpus. Skipped unless ``TALLY_TEST_POSTGRES_DSN``
is set, so a checkout with no infrastructure still runs the suite green:

    TALLY_TEST_POSTGRES_DSN=postgresql://tally:tally@localhost:5432/tally \\
      uv run --extra dev pytest -q tests/test_adopt_org_pg.py
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from gateway.adopt_org import AdoptError, OrgAdopter
from gateway.config import Settings

DSN = os.environ.get("TALLY_TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set TALLY_TEST_POSTGRES_DSN to a migrated control-plane database"
)

NAME_PREFIX = "adopt-org-test-"


class SpyKeyProvider:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def mint(self) -> str:  # pragma: no cover - adoption never mints
        raise AssertionError("adopt_org must never mint key material")

    def delete(self, ref: str) -> None:
        self.deleted.append(ref)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DSN, autocommit=True) as connection:
        yield connection
        # Teardown targets the name prefix only, never a UUID, so a typo cannot reach the corpus.
        with connection.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE name LIKE %s", (NAME_PREFIX + "%",))


def _make_tenant(conn: psycopg.Connection, *, org: str | None, ref: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants (name, region, plan, hash_salt_kek_ref, clerk_org_id)
            VALUES (%s, 'local', 'free', %s, %s) RETURNING id
            """,
            (f"{NAME_PREFIX}{uuid.uuid4().hex[:8]}", ref, org),
        )
        tenant_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO usage_limits (tenant_id, plan) VALUES (%s, 'free') "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )
    return tenant_id


def _org(conn: psycopg.Connection, tenant_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT clerk_org_id FROM tenants WHERE id = %s", (tenant_id,))
        row = cur.fetchone()
        return None if row is None else row[0]


def _exists(conn: psycopg.Connection, tenant_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,))
        return cur.fetchone() is not None


def _adopter(keys: SpyKeyProvider | None = None, spans: int = 0) -> OrgAdopter:
    return OrgAdopter(
        Settings(postgres_dsn=DSN),
        key_provider=keys or SpyKeyProvider(),
        span_counter=lambda _tid: spans,
    )


def test_the_hand_written_update_really_does_fail(conn: psycopg.Connection) -> None:
    """The premise. Without this the whole command is solving an imagined problem."""
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")

    with psycopg.connect(DSN) as isolated, isolated.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation, match="uq_tenants_clerk_org_id"):
            cur.execute("UPDATE tenants SET clerk_org_id = %s WHERE id = %s", (org, target))
        isolated.rollback()

    assert _org(conn, target) is None


def test_adoption_gets_past_the_index(conn: psycopg.Connection) -> None:
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    keys = SpyKeyProvider()

    report = _adopter(keys).run(clerk_org_id=org, target_tenant_id=target, confirm=True)

    assert report.applied is True
    assert _org(conn, target) == org
    assert not _exists(conn, incumbent)
    assert keys.deleted == ["local://hmac/incumbent/v1"]
    # The target's own key set is what its corpus was hashed under. It must be untouched.
    with conn.cursor() as cur:
        cur.execute("SELECT hash_salt_kek_ref FROM tenants WHERE id = %s", (target,))
        assert cur.fetchone()[0] == "local://hmac/target/v1"


def test_the_delete_cascades_every_child_row(conn: psycopg.Connection) -> None:
    """usage_limits is the row provisioning creates. Prove the cascade actually takes it."""
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM usage_limits WHERE tenant_id = %s", (incumbent,))
        assert cur.fetchone()[0] == 1

    _adopter().run(clerk_org_id=org, target_tenant_id=target, confirm=True)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM usage_limits WHERE tenant_id = %s", (incumbent,))
        assert cur.fetchone()[0] == 0


def test_the_census_reads_the_live_catalog(conn: psycopg.Connection) -> None:
    """Not a hardcoded list: the count must match what the database actually declares today."""
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")

    report = _adopter().run(clerk_org_id=org, target_tenant_id=target)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM pg_constraint
                 WHERE contype='f' AND confrelid='public.tenants'::regclass AND confdeltype='c')
            + (SELECT count(*) FROM information_schema.columns
                 WHERE table_schema='public' AND column_name='tenant_id'
                   AND table_name NOT IN (
                     SELECT conrelid::regclass::text FROM pg_constraint
                     WHERE contype='f' AND confrelid='public.tenants'::regclass))
            """
        )
        expected = int(cur.fetchone()[0])
    assert report.tables_examined == expected
    assert [c.table for c in report.census] == ["usage_limits"]


def test_dry_run_changes_nothing(conn: psycopg.Connection) -> None:
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    keys = SpyKeyProvider()

    report = _adopter(keys).run(clerk_org_id=org, target_tenant_id=target)

    assert report.applied is False
    assert _org(conn, target) is None
    assert _exists(conn, incumbent)
    assert keys.deleted == []


def test_a_failed_update_rolls_the_delete_back(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction. The incumbent must survive a failure after its DELETE was issued."""
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    keys = SpyKeyProvider()

    real_execute = psycopg.Cursor.execute

    def exploding(self, query, params=None, **kwargs):  # type: ignore[no-untyped-def]
        text = query if isinstance(query, str) else query.decode()
        if text.strip().startswith("UPDATE tenants SET clerk_org_id"):
            raise RuntimeError("injected failure between the delete and the update")
        return real_execute(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", exploding)
    with pytest.raises(RuntimeError, match="injected failure"):
        _adopter(keys).run(clerk_org_id=org, target_tenant_id=target, confirm=True)
    monkeypatch.undo()

    assert _exists(conn, incumbent), "the DELETE must have rolled back with the transaction"
    assert _org(conn, incumbent) == org, "the org must still point where it did"
    assert _org(conn, target) is None
    assert keys.deleted == [], "no key material may be destroyed by a rolled-back adoption"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM usage_limits WHERE tenant_id = %s", (incumbent,))
        assert cur.fetchone()[0] == 1, "the cascade must have rolled back too"


def test_running_it_twice_against_postgres_is_safe(conn: psycopg.Connection) -> None:
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    adopter = _adopter()

    first = adopter.run(clerk_org_id=org, target_tenant_id=target, confirm=True)
    second = adopter.run(clerk_org_id=org, target_tenant_id=target, confirm=True)

    assert first.applied is True
    assert second.already_adopted is True and second.applied is False
    assert _org(conn, target) == org


def test_a_populated_incumbent_refuses(conn: psycopg.Connection) -> None:
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO feature_tags (tenant_id, tag, description) VALUES (%s, 'x', 'y')",
            (incumbent,),
        )

    with pytest.raises(AdoptError, match="feature_tags=1"):
        _adopter().run(clerk_org_id=org, target_tenant_id=target, confirm=True)

    assert _exists(conn, incumbent)
    assert _org(conn, target) is None


def test_rows_in_an_unlinked_table_refuse(conn: psycopg.Connection) -> None:
    """No FK carries these away, so a delete would orphan them. Counted, and blocking."""
    org = f"org_{uuid.uuid4().hex}"
    target = _make_tenant(conn, org=None, ref="local://hmac/target/v1")
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scheduler_runs (tenant_id, job_name, status) "
            "VALUES (%s, 'stitcher', 'failed')",
            (incumbent,),
        )
    try:
        with pytest.raises(AdoptError, match="scheduler_runs=1"):
            _adopter().run(clerk_org_id=org, target_tenant_id=target, confirm=True)
        assert _exists(conn, incumbent)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scheduler_runs WHERE tenant_id = %s", (incumbent,))


def test_missing_target_refuses_against_postgres(conn: psycopg.Connection) -> None:
    org = f"org_{uuid.uuid4().hex}"
    incumbent = _make_tenant(conn, org=org, ref="local://hmac/incumbent/v1")

    with pytest.raises(AdoptError, match="no tenant"):
        _adopter().run(clerk_org_id=org, target_tenant_id=str(uuid.uuid4()), confirm=True)

    assert _exists(conn, incumbent), "a bad target must not cost the incumbent its life"
    assert _org(conn, incumbent) == org
