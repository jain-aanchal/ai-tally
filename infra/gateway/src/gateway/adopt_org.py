# SPDX-License-Identifier: Apache-2.0
"""Point a Clerk organization at an EXISTING tenant, disposing of the one its webhook created.

WHY this exists (CTO-364, specced in ``docs/nova-demo-tenant.md``). Attaching a Clerk org to a
tenant that already holds a corpus looks like one UPDATE, and locally it is, because Clerk cannot
reach ``localhost`` so ``organization.created`` never fires and nothing claims the org id. In
production it fires within seconds, ``tenant_provisioning`` INSERTs a fresh empty tenant that claims
the org id, and the UPDATE then dies on ``uq_tenants_clerk_org_id`` (a partial unique index on
non-null values). Recovering by hand means a cascading DELETE followed by an UPDATE, in order,
against production Postgres, where the DELETE is one typo away from the corpus itself. This module
is that procedure, written once, checked, and run inside a single transaction.

WHY A COMMAND AND NOT AN ENDPOINT. ``CLAUDE.md``'s "control-plane writes go through gateway
endpoints" governs the request path: the web app must never open Postgres itself. Operator
maintenance already has a different established shape here, ``gateway/seed.py``, and this follows it.
Moving ``clerk_org_id`` between tenants is a tenant-takeover primitive: an endpoint for it would let
anyone holding the control-plane service token, which the web app holds, point their own Clerk org at
any customer's data. Shell access to the gateway task is the right privilege level for something run
once under supervision.

THE INVARIANTS THIS MODULE HOLDS.

* **One transaction.** The DELETE of the incumbent and the UPDATE of the target commit together or
  not at all. An org pointing nowhere is worse than an adoption that failed and changed nothing.

* **Dry run by default.** Nothing is written without ``--confirm``. The census below is the operator's
  evidence that the row about to be deleted is the empty one.

* **Honest under uncertainty.** Emptiness is measured, never assumed: the census is derived from the
  live catalog, so a migration that adds a table cannot leave this tool silently checking a stale
  list. A telemetry count that could not be read is reported as unknown and refuses the adoption
  rather than being treated as zero.

* **The target's HMAC key is never touched.** Every account and user hash in the target's corpus was
  computed under ``tenants.hash_salt_kek_ref`` as it stands. Adoption rewrites ``clerk_org_id`` and
  nothing else, so the corpus stays joinable to itself. The DISCARDED tenant's key set is deleted
  after commit, and only because the census proved nothing was ever hashed under it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
import uuid
from dataclasses import dataclass, field

import psycopg

from gateway.config import Settings, get_settings
from gateway.tenant_provisioning import KeyMaterialProvider, build_key_provider

logger = logging.getLogger(__name__)


class AdoptError(RuntimeError):
    """The adoption was refused. The message says why, and nothing was written."""


#: Tables whose rows for the incumbent tenant are what provisioning itself creates, so finding them
#: is expected and does not block disposal. ``tenant_provisioning.provision`` writes exactly one
#: ``usage_limits`` row alongside the tenant; everything else on a freshly provisioned tenant is
#: empty, including its ingest keys (the provisioning checklist's finding #6).
DISPOSABLE_TABLES = frozenset({"usage_limits"})


@dataclass(frozen=True, slots=True)
class TenantRow:
    """The columns of ``tenants`` this command reasons about."""

    id: str
    name: str
    clerk_org_id: str | None
    plan: str
    hash_salt_kek_ref: str
    created_at: _dt.datetime

    def describe(self) -> str:
        org = self.clerk_org_id or "(none)"
        return f"{self.id}  name={self.name!r}  clerk_org_id={org}  plan={self.plan}"


@dataclass(frozen=True, slots=True)
class CensusRow:
    """One table's row count for the tenant about to be discarded."""

    table: str
    rows: int
    #: True when a FK to ``tenants`` carries the rows away on DELETE. False means the rows would be
    #: ORPHANED by a delete, which is why they are counted separately and why any non-zero refuses.
    cascades: bool


@dataclass(slots=True)
class AdoptReport:
    """What the command found and, when confirmed, what it did. Rendered for an incident log."""

    clerk_org_id: str
    target_before: TenantRow
    target_after: TenantRow | None = None
    incumbent: TenantRow | None = None
    census: list[CensusRow] = field(default_factory=list)
    tables_examined: int = 0
    span_count: int | None = None
    span_count_error: str = ""
    applied: bool = False
    already_adopted: bool = False
    key_ref_deleted: str = ""
    key_ref_orphaned: str = ""
    notes: list[str] = field(default_factory=list)


def _parse_uuid(value: str) -> str:
    """Accept the tenant UUID and ONLY the UUID.

    ``tenant_lookup.resolve_tenant_uuid`` would also take a name or a Clerk org id, which is right for
    a control-plane read and wrong here. This command deletes a row; a name that resolves to a
    neighbouring tenant is precisely the accident it exists to prevent, so the operator has to type
    the canonical identifier.
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise AdoptError(
            f"--tenant must be the tenant UUID, not {value!r}. This command deletes a row, so it "
            "does not accept a name or an org id that could resolve to the wrong tenant."
        ) from None


def _clean_org(value: str) -> str:
    trimmed = (value or "").strip()
    if not trimmed:
        raise AdoptError("--clerk-org must be non-empty")
    return trimmed


def _row(record: tuple) -> TenantRow:
    return TenantRow(
        id=str(record[0]),
        name=str(record[1]),
        clerk_org_id=record[2],
        plan=str(record[3]),
        hash_salt_kek_ref=str(record[4]),
        created_at=record[5],
    )


_TENANT_COLUMNS = "id, name, clerk_org_id, plan, hash_salt_kek_ref, created_at"


def clickhouse_span_count(settings: Settings, tenant_id: str) -> int:
    """Spans stored under ``tenant_id`` in ClickHouse. Raises if ClickHouse cannot be read.

    Deliberately raises rather than returning 0 on failure: "we could not reach ClickHouse" and "this
    tenant has no telemetry" are different answers, and only one of them makes a tenant safe to
    delete.
    """
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_db,
    )
    try:
        result = client.query(
            "SELECT count() FROM otel_spans WHERE TenantId = {tid:String}",
            parameters={"tid": tenant_id},
        )
        return int(result.result_rows[0][0])
    finally:
        client.close()


class OrgAdopter:
    """Runs the adoption. The key provider and the span counter are injected so tests can drive it."""

    def __init__(
        self,
        settings: Settings,
        key_provider: KeyMaterialProvider | None = None,
        span_counter=None,
    ) -> None:
        self._settings = settings
        self._dsn = settings.postgres_dsn
        self._key_provider = key_provider
        self._span_counter = span_counter or (
            lambda tenant_id: clickhouse_span_count(settings, tenant_id)
        )

    def _keys(self) -> KeyMaterialProvider:
        # Built lazily: a dry run never needs a key provider, and building the AWS one can fail on a
        # box with no credentials. Refusing to preview because of that would be wrong.
        if self._key_provider is None:
            self._key_provider = build_key_provider(self._settings)
        return self._key_provider

    # -- catalog -------------------------------------------------------------------------------

    @staticmethod
    def _cascading_children(cur) -> list[str]:
        """Tables carried away when a ``tenants`` row is deleted, read from the live catalog.

        Read rather than hardcoded so a migration that adds a per-tenant table is covered the day it
        lands. ``docs/nova-demo-tenant.md`` says "about thirty tables" from a manual count; the
        catalog is the only figure that stays true.
        """
        cur.execute(
            """
            SELECT c.conrelid::regclass::text
            FROM pg_constraint c
            WHERE c.contype = 'f'
              AND c.confrelid = 'public.tenants'::regclass
              AND c.confdeltype = 'c'
            ORDER BY 1
            """
        )
        return [str(r[0]) for r in cur.fetchall()]

    @staticmethod
    def _unlinked_tenant_tables(cur) -> list[str]:
        """Tables carrying a ``tenant_id`` with NO foreign key to ``tenants``.

        These exist (the export watermarks, the scheduler and reconciliation run logs, the ingest
        cursors and the batch idempotency ledger) and they hold ``tenant_id`` as TEXT, so a DELETE of
        the tenant leaves their rows behind as orphans. The manual count that produced "about thirty
        tables" missed them entirely. They cannot be cascaded, so any row here refuses the disposal
        instead of quietly orphaning it.
        """
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name = 'tenant_id'
              AND c.table_name NOT IN (
                SELECT f.conrelid::regclass::text
                FROM pg_constraint f
                WHERE f.contype = 'f' AND f.confrelid = 'public.tenants'::regclass
              )
            ORDER BY 1
            """
        )
        return [str(r[0]) for r in cur.fetchall()]

    def _census(self, cur, tenant_id: str) -> tuple[list[CensusRow], int]:
        """Count every per-tenant row the incumbent owns. Returns the non-empty ones and the total
        number of tables examined."""
        found: list[CensusRow] = []
        cascading = self._cascading_children(cur)
        unlinked = self._unlinked_tenant_tables(cur)
        for table in cascading:
            cur.execute(f'SELECT count(*) FROM "{table}" WHERE tenant_id = %s', (tenant_id,))
            rows = int(cur.fetchone()[0])
            if rows:
                found.append(CensusRow(table=table, rows=rows, cascades=True))
        for table in unlinked:
            # tenant_id is TEXT on these, so compare as text rather than casting to uuid.
            cur.execute(f'SELECT count(*) FROM "{table}" WHERE tenant_id = %s', (tenant_id,))
            rows = int(cur.fetchone()[0])
            if rows:
                found.append(CensusRow(table=table, rows=rows, cascades=False))
        return found, len(cascading) + len(unlinked)

    # -- the operation -------------------------------------------------------------------------

    def run(
        self,
        *,
        clerk_org_id: str,
        target_tenant_id: str,
        confirm: bool = False,
        allow_reassign: bool = False,
        check_telemetry: bool = True,
    ) -> AdoptReport:
        org = _clean_org(clerk_org_id)
        target_id = _parse_uuid(target_tenant_id)

        conn = psycopg.connect(self._dsn)
        try:
            with conn.cursor() as cur:
                # EXCLUSIVE blocks writers, including a webhook provisioning this very org id
                # between our DELETE and our UPDATE, while still allowing plain SELECTs so the
                # dashboard keeps reading. Serialising against provisioning is the whole point:
                # the race is what breaks the hand-written version.
                cur.execute("LOCK TABLE tenants IN EXCLUSIVE MODE")

                cur.execute(f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = %s", (target_id,))
                record = cur.fetchone()
                if record is None:
                    raise AdoptError(
                        f"no tenant {target_id}. Nothing was written. Check the UUID against "
                        "`SELECT id, name FROM tenants`."
                    )
                target = _row(record)
                report = AdoptReport(clerk_org_id=org, target_before=target)

                cur.execute(
                    f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE clerk_org_id = %s", (org,)
                )
                record = cur.fetchone()
                incumbent = _row(record) if record is not None else None

                # Idempotent exit: the org already points where it should. No writes, no lock held
                # any longer than the read, exit 0.
                if incumbent is not None and incumbent.id == target.id:
                    report.already_adopted = True
                    report.notes.append(
                        f"{org} already resolves to {target.id}. Nothing to do."
                    )
                    conn.rollback()
                    return report

                # Ambiguity: the target already answers to a DIFFERENT org. Moving it would silently
                # orphan that org's workspace, so it takes an explicit flag.
                if target.clerk_org_id is not None and target.clerk_org_id != org:
                    if not allow_reassign:
                        raise AdoptError(
                            f"tenant {target.id} already carries clerk_org_id "
                            f"{target.clerk_org_id}, not {org}. Adopting would take that "
                            "organization's workspace away from it. Nothing was written. If the "
                            "move is intended, re-run with --allow-reassign."
                        )
                    report.notes.append(
                        f"--allow-reassign: {target.clerk_org_id} is being moved off "
                        f"{target.id} and replaced by {org}. That organization now resolves to "
                        "no tenant and its next sign-in provisions a fresh empty one."
                    )

                if incumbent is not None:
                    report.incumbent = incumbent
                    census, examined = self._census(cur, incumbent.id)
                    report.census = census
                    report.tables_examined = examined

                    blocking = [c for c in census if c.table not in DISPOSABLE_TABLES]
                    if blocking:
                        detail = ", ".join(f"{c.table}={c.rows}" for c in blocking)
                        raise AdoptError(
                            f"tenant {incumbent.id} holds {org} and is NOT empty ({detail}). "
                            "This command only disposes of a tenant that provisioning just "
                            "created. Nothing was written. Work out what that tenant is before "
                            "going further."
                        )

                    if check_telemetry:
                        try:
                            report.span_count = self._span_counter(incumbent.id)
                        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                            report.span_count_error = f"{type(exc).__name__}: {exc}"
                            raise AdoptError(
                                f"could not count spans for {incumbent.id} in ClickHouse "
                                f"({report.span_count_error}). An unreadable count is not a zero "
                                "count, so the tenant cannot be shown to be disposable. Nothing "
                                "was written. Fix the connection, or pass "
                                "--skip-telemetry-check if you have established another way that "
                                "the tenant has no telemetry."
                            ) from exc
                        if report.span_count:
                            raise AdoptError(
                                f"tenant {incumbent.id} holds {org} and has "
                                f"{report.span_count:,} spans in ClickHouse. Deleting it would "
                                "strand that telemetry with no control-plane row. Nothing was "
                                "written."
                            )
                    else:
                        report.notes.append(
                            "--skip-telemetry-check: the ClickHouse span count for the discarded "
                            "tenant was NOT checked."
                        )

                if not confirm:
                    conn.rollback()
                    return report

                if incumbent is not None:
                    cur.execute("DELETE FROM tenants WHERE id = %s", (incumbent.id,))
                    if cur.rowcount != 1:
                        raise AdoptError(
                            f"expected to delete exactly 1 tenant row for {incumbent.id}, "
                            f"deleted {cur.rowcount}. Rolled back."
                        )
                cur.execute(
                    "UPDATE tenants SET clerk_org_id = %s WHERE id = %s", (org, target.id)
                )
                if cur.rowcount != 1:
                    raise AdoptError(
                        f"expected to update exactly 1 tenant row for {target.id}, updated "
                        f"{cur.rowcount}. Rolled back."
                    )
                cur.execute(f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = %s", (target.id,))
                report.target_after = _row(cur.fetchone())
                conn.commit()
                report.applied = True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        # After commit, and only here. The key set belongs to a tenant that the census proved held no
        # rows and no spans, so nothing was ever hashed under it and deleting it destroys no ability
        # to reproduce a hash. Doing it before the commit would delete live key material if the
        # transaction then rolled back; a failure here leaves recoverable material, not a broken
        # tenant, so it is reported and not raised (the shape tenant_provisioning uses too).
        if report.incumbent is not None:
            ref = report.incumbent.hash_salt_kek_ref
            try:
                self._keys().delete(ref)
                report.key_ref_deleted = ref
            except Exception as exc:  # noqa: BLE001 - cleanup must not undo a committed adoption
                report.key_ref_orphaned = ref
                logger.error(
                    "adopted %s but the discarded tenant's HMAC key reference %s could not be "
                    "deleted (%s): delete it by hand",
                    org,
                    ref,
                    exc,
                )
        return report


def render(report: AdoptReport) -> str:
    """The operator's record of the run, written to be pasted into an incident log."""
    stamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = [
        f"adopt_org {stamp}",
        f"  clerk org      : {report.clerk_org_id}",
        f"  target tenant  : {report.target_before.describe()}",
    ]
    if report.already_adopted:
        lines.append("  result         : ALREADY ADOPTED, nothing written")
        for note in report.notes:
            lines.append(f"  note           : {note}")
        return "\n".join(lines)

    if report.incumbent is None:
        lines.append(f"  incumbent      : none, {report.clerk_org_id} is unclaimed")
    else:
        lines.append(f"  incumbent      : {report.incumbent.describe()}")
        lines.append(f"                   created {report.incumbent.created_at.isoformat()}")
        lines.append(
            f"  census         : {report.tables_examined} per-tenant tables examined, "
            f"{len(report.census)} non-empty"
        )
        for entry in report.census:
            carry = "cascades on delete" if entry.cascades else "NO FK, would orphan"
            lines.append(f"                   {entry.table}: {entry.rows} row(s), {carry}")
        if report.span_count is not None:
            lines.append(f"  clickhouse     : {report.span_count:,} spans under the incumbent")
        elif report.span_count_error:
            lines.append(f"  clickhouse     : UNKNOWN ({report.span_count_error})")
        else:
            lines.append("  clickhouse     : not checked (--skip-telemetry-check)")

    if not report.applied:
        lines.append("  result         : DRY RUN, nothing written. Re-run with --confirm to apply.")
        if report.incumbent is not None:
            lines.append(f"                   would DELETE tenant {report.incumbent.id}")
        lines.append(
            f"                   would SET clerk_org_id={report.clerk_org_id} on "
            f"{report.target_before.id}"
        )
    else:
        lines.append("  result         : ADOPTED, committed")
        if report.incumbent is not None:
            lines.append(f"                   deleted tenant {report.incumbent.id}")
        after = report.target_after
        if after is not None:
            lines.append(f"                   tenant after: {after.describe()}")
        lines.append(
            f"                   target hash_salt_kek_ref unchanged: "
            f"{report.target_before.hash_salt_kek_ref}"
        )
        if report.key_ref_deleted:
            lines.append(f"                   discarded HMAC key deleted: {report.key_ref_deleted}")
        if report.key_ref_orphaned:
            lines.append(
                f"                   HMAC key {report.key_ref_orphaned} could NOT be deleted; "
                "delete it by hand"
            )
    for note in report.notes:
        lines.append(f"  note           : {note}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.adopt_org",
        description=(
            "Point a Clerk organization at an existing tenant, deleting the empty tenant its "
            "webhook created. Dry run unless --confirm is given."
        ),
    )
    parser.add_argument("--clerk-org", required=True, help="the Clerk organization id (org_...)")
    parser.add_argument(
        "--tenant", required=True, help="the target tenant UUID, the one holding the corpus"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually write. Without it the command reports what it would do and exits.",
    )
    parser.add_argument(
        "--allow-reassign",
        action="store_true",
        help="permit adopting a target that already carries a DIFFERENT clerk_org_id",
    )
    parser.add_argument(
        "--skip-telemetry-check",
        action="store_true",
        help="do not count the discarded tenant's spans in ClickHouse before deleting it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    adopter = OrgAdopter(get_settings())
    try:
        report = adopter.run(
            clerk_org_id=args.clerk_org,
            target_tenant_id=args.tenant,
            confirm=args.confirm,
            allow_reassign=args.allow_reassign,
            check_telemetry=not args.skip_telemetry_check,
        )
    except AdoptError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
