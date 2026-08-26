# SPDX-License-Identifier: Apache-2.0
"""What a tenant intends to spend on AI, per period and per scope (CTO-205, F1).

WHY this exists. Nothing in this system has ever recorded a customer's spending intent. The two
``daily_budget_usd`` columns already in the schema (``tenant_replay_config``, ``tenant_eval_config``)
cap what ai-tally itself spends running replays and evals; they say nothing about the customer's own
AI bill. Every "versus budget" figure in the spend-forecasting epic (CTO-204) depends on this table,
so it is the first thing that has to exist. See ``db/postgres/0026_tenant_budgets.sql`` for the full
rationale and ``docs/spend-forecasting-scope.md`` for where it is heading.

THE RULE OF THIS MODULE: a tenant with no budget row is a normal state, not missing config, and
never an implicit zero. :meth:`TenantBudgetStore.list` returning ``[]`` is the state every tenant on
this system is in today. Downstream renders "no budget set" and omits the variance entirely rather
than reporting a tenant as infinitely over a budget of zero. A stored zero is a different and real
claim ("this scope may spend nothing"), which is why ``amount_micro >= 0`` rather than ``> 0``.

WHY overlapping budgets are refused at write time. Two budgets covering the same scope, period and
day leave the burn-down with no principled answer about which number to draw the line at, and every
tie-break is arbitrary: newest-wins silently disables a budget somebody set, smallest-wins turns a
duplicate into a false breach, summing invents a budget nobody approved. Worse, whichever rule were
chosen would have to be reimplemented identically in the projection, the chart and the future breach
alerts, and the first drift between them puts an alert and a chart that disagree on one screen. So
the write is refused and the caller is told which budget it collided with. Downstream may rely on:
for one ``(tenant, period, scope_kind, scope_value)`` and one day, AT MOST ONE budget applies.

The pre-check in :meth:`TenantBudgetStore.upsert` exists for the error MESSAGE, not for correctness.
Two concurrent POSTs would both find no conflict and both insert; the EXCLUDE constraint in
migration 0026 is what actually holds the invariant, and this module translates its violation into
the same 409 the pre-check produces.

Money is integer micro-USD throughout, as everywhere in this codebase. There is no float on this
path, and the API neither accepts nor emits dollars.

Reads and writes go through ``GET/POST/DELETE /v1/tenant/budgets``. The web app never touches
Postgres directly, same rule as :mod:`gateway.tenant_account_labels` and
:mod:`gateway.tenant_allocation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg import errors as pg_errors

from gateway.config import Settings
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

#: Budget periods, matching the CHECK constraint in migration 0026. Deliberately only the two the
#: forecast can actually evaluate: a period the projection cannot compute a window for would be a
#: budget that never renders. ``year`` is absent for that reason, not by oversight.
BUDGET_PERIODS: tuple[str, ...] = ("month", "quarter")

#: What a budget applies to. Each kind names a dimension the cost queries in
#: ``web/lib/clickhouse.ts`` already group by, so a scoped budget is always comparable against a
#: series that exists. ``tenant`` is the whole bill and takes an empty ``scope_value``.
BUDGET_SCOPE_KINDS: tuple[str, ...] = ("tenant", "feature", "model", "layer")

#: The scope kind that covers everything and therefore names nothing.
TENANT_SCOPE_KIND = "tenant"

#: A budget_id is a handle in a URL and an audit line, not a description.
MAX_BUDGET_ID_CHARS = 120

#: A scope value is a FeatureId, a model id or a layer name. All are short identifiers.
MAX_SCOPE_VALUE_CHARS = 200

#: Ceiling on a stored budget: 1e15 micro-USD is one billion dollars. Not a business rule, a typo
#: guard. A budget is a BIGINT and an accidental extra six zeros would otherwise sail through and
#: make every variance downstream meaningless while looking like a real number.
MAX_AMOUNT_MICRO = 1_000_000_000_000_000


class BudgetError(ValueError):
    """Caller-facing validation error. Surfaces as HTTP 422."""


#: The caller's tenant identifier matches no ``tenants`` row. Re-exported from
#: :mod:`gateway.tenant_lookup` rather than redefined, so a caller catching a budget-store failure
#: catches one name and the endpoint can map it to 404 without knowing which module raised it. This
#: module deliberately does NOT add a fifth private copy of the name-vs-UUID resolver: CTO-201
#: tracks folding the remaining copies in :mod:`gateway.tenant_identity` and
#: :mod:`gateway.tenant_account_labels` onto this one, and that consolidation is not this ticket.
TenantNotFound = TenantNotFoundError


class BudgetOverlapError(BudgetError):
    """A budget already covers this scope, period and date range. Surfaces as HTTP 409.

    Carries ``conflicting_budget_id`` when we know it, because "that overlaps" is not actionable
    and "that overlaps with research-agent-q1" is. The constraint-violation path does not know it,
    so the field is optional and the caller must handle it being None.
    """

    def __init__(self, message: str, conflicting_budget_id: str | None = None) -> None:
        super().__init__(message)
        self.conflicting_budget_id = conflicting_budget_id


@dataclass(frozen=True, slots=True)
class Budget:
    """One stored budget row."""

    budget_id: str
    period: str
    amount_micro: int
    scope_kind: str
    scope_value: str
    starts_on: date
    ends_on: date | None
    created_at: datetime | None
    updated_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "budget_id": self.budget_id,
            "period": self.period,
            "amount_micro": self.amount_micro,
            "scope_kind": self.scope_kind,
            # Always present, always a string, '' for a tenant-wide budget. A consumer that has to
            # branch on null vs '' for the same concept will eventually get it wrong.
            "scope_value": self.scope_value,
            "starts_on": self.starts_on.isoformat(),
            # null means open-ended, not unknown. The one nullable field here, and it is a real
            # statement rather than missing data.
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def normalize_budget_id(value: object) -> str:
    """Validate the caller's stable handle for a budget.

    Trim only, no case folding: it is the caller's own identifier and it is the primary key, so
    rewriting it would mean a caller could not address the row it just created.
    """
    if not isinstance(value, str):
        raise BudgetError("budget_id must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise BudgetError("budget_id must be non-empty")
    if len(trimmed) > MAX_BUDGET_ID_CHARS:
        raise BudgetError(f"budget_id must be at most {MAX_BUDGET_ID_CHARS} characters")
    return trimmed


def normalize_period(value: object) -> str:
    """Validate a period against :data:`BUDGET_PERIODS`.

    An unknown period is rejected rather than coerced to 'month'. Storing one period and evaluating
    another would draw a burn-down over the wrong window and label it with the right one.
    """
    if not isinstance(value, str):
        raise BudgetError("period must be a string")
    trimmed = value.strip().lower()
    if trimmed not in BUDGET_PERIODS:
        raise BudgetError("period must be one of: " + ", ".join(BUDGET_PERIODS))
    return trimmed


def normalize_amount_micro(value: object) -> int:
    """Validate a budget amount in micro-USD.

    Rejects floats outright, including whole-valued ones. Accepting ``30000.0`` would advertise
    that dollars-as-float is a supported input shape, and the next caller passes ``30000.5`` and
    silently loses half a cent per budget. Money in this system is an integer of micro-USD, and the
    boundary is the right place to say so.

    Zero is accepted: a scope that may spend nothing is a real budget. Absence of a row, not a
    zero, is what means "no budget set".
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetError("amount_micro must be an integer of micro-USD (no floats, no dollars)")
    if value < 0:
        raise BudgetError("amount_micro must be >= 0")
    if value > MAX_AMOUNT_MICRO:
        raise BudgetError(
            f"amount_micro must be at most {MAX_AMOUNT_MICRO} "
            "(1e15 micro-USD = $1B); check for a misplaced decimal"
        )
    return value


def normalize_scope(scope_kind: object, scope_value: object) -> tuple[str, str]:
    """Validate the two halves of a scope together, because they constrain each other.

    A ``tenant`` budget must name nothing and every other kind must name something. Validating them
    separately would admit 'tenant'/'checkout' (ambiguous about what it covers) and 'feature'/''
    (never matchable against a series), both of which the CHECK constraint would then reject as an
    opaque 503.
    """
    if not isinstance(scope_kind, str):
        raise BudgetError("scope_kind must be a string")
    kind = scope_kind.strip().lower()
    if kind not in BUDGET_SCOPE_KINDS:
        raise BudgetError("scope_kind must be one of: " + ", ".join(BUDGET_SCOPE_KINDS))

    if scope_value is None:
        value = ""
    elif isinstance(scope_value, str):
        value = scope_value.strip()
    else:
        raise BudgetError("scope_value must be a string when provided")

    if kind == TENANT_SCOPE_KIND:
        if value:
            raise BudgetError("a tenant-wide budget must not name a scope_value")
        return kind, ""
    if not value:
        raise BudgetError(f"a {kind}-scoped budget requires a scope_value")
    if len(value) > MAX_SCOPE_VALUE_CHARS:
        raise BudgetError(f"scope_value must be at most {MAX_SCOPE_VALUE_CHARS} characters")
    # No case folding. A FeatureId or a model id is compared against telemetry that preserves case,
    # so lowercasing here would produce a budget that matches nothing.
    return kind, value


def normalize_date(value: object, *, field: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` date."""
    if isinstance(value, datetime):
        raise BudgetError(f"{field} must be a date (YYYY-MM-DD), not a timestamp")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise BudgetError(f"{field} must be an ISO date string (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BudgetError(f"{field} must be an ISO date string (YYYY-MM-DD)") from exc


def normalize_optional_date(value: object, *, field: str) -> date | None:
    """Parse an optional end date. ``None`` means open-ended, which is the common case.

    Open-ended is not "unknown". A monthly budget is usually a standing figure that runs until
    somebody changes it, so absence here is a statement rather than missing data.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return normalize_date(value, field=field)


_COLUMNS = (
    "budget_id, period, amount_micro, scope_kind, scope_value, "
    "starts_on, ends_on, created_at, updated_at"
)


def _row_to_budget(row: tuple) -> Budget:
    return Budget(
        budget_id=str(row[0]),
        period=str(row[1]),
        amount_micro=int(row[2]),
        scope_kind=str(row[3]),
        scope_value=str(row[4]),
        starts_on=row[5],
        ends_on=row[6],
        created_at=row[7],
        updated_at=row[8],
    )


class TenantBudgetStore:
    """Postgres-backed CRUD over ``tenant_budgets``.

    Every method takes the ``tenant_id`` resolved by upstream auth and folds it onto ``tenants.id``,
    so the SQL never crosses tenants.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[Budget]:
        """Every budget this tenant has set.

        An empty list is the normal answer for every tenant on this system today. It means "no
        budget set", and the caller must render that rather than substituting zero.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                f"SELECT {_COLUMNS} FROM tenant_budgets WHERE tenant_id = %s "
                "ORDER BY scope_kind, scope_value, starts_on, budget_id",
                (resolved,),
            )
            return [_row_to_budget(row) for row in cur.fetchall()]

    def get(self, tenant_id: str, budget_id: str) -> Budget | None:
        """One budget, or ``None`` when the tenant has not set it. ``None`` is not an error."""
        budget_id = normalize_budget_id(budget_id)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                f"SELECT {_COLUMNS} FROM tenant_budgets "
                "WHERE tenant_id = %s AND budget_id = %s",
                (resolved, budget_id),
            )
            row = cur.fetchone()
            return _row_to_budget(row) if row else None

    def upsert(
        self,
        tenant_id: str,
        *,
        budget_id: str,
        period: str,
        amount_micro: int,
        scope_kind: str,
        starts_on: object,
        scope_value: object = "",
        ends_on: object = None,
    ) -> Budget:
        """Create or replace one budget. Refuses an overlapping one.

        Setting and editing are the same call: the write is an upsert on
        ``(tenant_id, budget_id)``, so a caller correcting an amount does not have to know whether
        the row exists, and two concurrent writers cannot produce two rows for one budget_id.

        Overlap is checked twice, and the two checks are not redundant. The SELECT below produces
        the useful error (it names the budget it collided with) but cannot be relied on for
        correctness, because two concurrent inserts both pass it. The EXCLUDE constraint in
        migration 0026 is the actual guarantee, and its violation is translated into the same
        :class:`BudgetOverlapError` so the caller sees one behaviour whichever path caught it.

        The self-exclusion in the pre-check (``budget_id <> %s``) is what makes editing a budget in
        place work at all: a budget always overlaps itself, so without it no budget could ever be
        updated.
        """
        budget_id = normalize_budget_id(budget_id)
        period = normalize_period(period)
        amount_micro = normalize_amount_micro(amount_micro)
        scope_kind, scope_value = normalize_scope(scope_kind, scope_value)
        start = normalize_date(starts_on, field="starts_on")
        end = normalize_optional_date(ends_on, field="ends_on")
        if end is not None and end < start:
            raise BudgetError("ends_on must be on or after starts_on")

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT budget_id FROM tenant_budgets
                WHERE tenant_id = %s
                  AND period = %s
                  AND scope_kind = %s
                  AND scope_value = %s
                  AND budget_id <> %s
                  AND daterange(starts_on, COALESCE(ends_on, 'infinity'::date), '[]')
                      && daterange(%s, COALESCE(%s, 'infinity'::date), '[]')
                LIMIT 1
                """,
                (resolved, period, scope_kind, scope_value, budget_id, start, end),
            )
            clash = cur.fetchone()
            if clash is not None:
                raise BudgetOverlapError(
                    "a budget already covers this scope and period over an overlapping date "
                    f"range: {clash[0]}",
                    conflicting_budget_id=str(clash[0]),
                )
            try:
                cur.execute(
                    f"""
                    INSERT INTO tenant_budgets
                        (tenant_id, budget_id, period, amount_micro,
                         scope_kind, scope_value, starts_on, ends_on)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, budget_id) DO UPDATE
                      SET period       = EXCLUDED.period,
                          amount_micro = EXCLUDED.amount_micro,
                          scope_kind   = EXCLUDED.scope_kind,
                          scope_value  = EXCLUDED.scope_value,
                          starts_on    = EXCLUDED.starts_on,
                          ends_on      = EXCLUDED.ends_on,
                          updated_at   = now()
                    RETURNING {_COLUMNS}
                    """,
                    (
                        resolved,
                        budget_id,
                        period,
                        amount_micro,
                        scope_kind,
                        scope_value,
                        start,
                        end,
                    ),
                )
            except pg_errors.ExclusionViolation as exc:
                # Lost the race against a concurrent writer. Same answer as the pre-check, minus
                # the colliding budget_id, which the constraint does not hand us.
                conn.rollback()
                raise BudgetOverlapError(
                    "a budget already covers this scope and period over an overlapping date range"
                ) from exc
            row = cur.fetchone()
            assert row is not None  # RETURNING on an upsert that cannot no-op
            conn.commit()
            return _row_to_budget(row)

    def delete(self, tenant_id: str, budget_id: str) -> bool:
        """Remove one budget, returning that scope to the "no budget set" state.

        A real DELETE. A budget is the tenant's own statement of intent, and withdrawing it should
        leave nothing behind claiming they still intend it.

        Returns True if this call removed a row, False if it was already absent. Deleting an absent
        budget is not an error, so a double-click cannot produce a 404.
        """
        budget_id = normalize_budget_id(budget_id)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "DELETE FROM tenant_budgets WHERE tenant_id = %s AND budget_id = %s",
                (resolved, budget_id),
            )
            removed = cur.rowcount > 0
            conn.commit()
            return removed
