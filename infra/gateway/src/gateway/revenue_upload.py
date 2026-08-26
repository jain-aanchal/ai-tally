# SPDX-License-Identifier: Apache-2.0
"""CSV revenue upload (CTO-198, plan item E5).

Revenue connectors cover Stripe and HubSpot. Plenty of B2B companies bill through Chargebee,
Recurly, Zuora, NetSuite or plain invoices, and the revenue truth sits in a spreadsheet finance
already maintains. Uploading ``account_id, period, amount, currency`` is the fastest path from zero
to a populated margin column and it works regardless of billing stack.

The uploaded rows become ordinary ``business_events`` rows — same table, same columns, same
``ValueType`` discriminator every other revenue source uses — so nothing downstream special-cases
them. In particular they flow through the CTO-194 revenue-source policy unchanged: a tenant who has
narrowed ``revenue_sources`` will see uploaded revenue only if they name this source, which is the
same rule Stripe and HubSpot live under.

Two things this has to get right.

**1. It is a point-in-time snapshot, not a feed.**
Revenue changes monthly and an upload nobody refreshes goes stale silently, quietly corrupting every
margin number that reads it. The upload therefore also writes a manifest row per period
(``tenant_revenue_uploads``) carrying ``uploaded_at``, and the dashboard renders an "as of" date and
a staleness badge off it. Without that, a six-month-old spreadsheet reads exactly like today's.

**2. Re-uploading the same period must REPLACE, not append.**
If it appends, revenue doubles on the second upload and nobody notices until margin looks
impossibly good. Three things make that structurally impossible rather than a convention:

* ``BusinessEventId`` is *derived*, not generated: ``csv_upload:<period>:<account_hash>``. Two
  uploads of the same account and period produce the same id, and ``business_events`` is a
  ``ReplacingMergeTree`` ordered on ``(TenantId, BusinessEventId)``, so a duplicate collapses.
* Every write is a DELETE of the period's existing rows followed by the INSERT, in that order,
  scoped by ``Source`` and by the derived-id prefix. That is what handles the case the id alone
  cannot: an account present in the first upload and absent from the second must *disappear*, not
  linger. It also means the reader sees the right total immediately, without waiting for a merge.
* The manifest's primary key is ``(tenant_id, period)``. A period cannot hold two snapshots.

Partial-file behaviour is deliberately **all-or-nothing**. Because an upload REPLACES the period,
accepting the good rows of a broken file would swap a complete prior snapshot for an incomplete one
and silently delete the revenue of every account whose row failed to parse. That is the exact
class of quiet corruption this module exists to prevent, so a single bad row rejects the file, and
the rejection names every offending line number at once so the file is fixed in one pass.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import psycopg

from gateway.config import Settings
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

UTC = timezone.utc

# business_events.Source stamped on every uploaded row. Also the scope of the delete half of a
# replace, so an upload can never reach a row a real connector wrote.
UPLOAD_SOURCE = "csv_upload"

# business_events.EventName. Descriptive rather than an outcome name: these rows are a period
# total, not a conversion, and the /features event list should read as such.
UPLOAD_EVENT_NAME = "revenue_snapshot"

# Prefix of every derived BusinessEventId. See the module docstring for why the id is derived.
ID_PREFIX = "csv_upload"

REQUIRED_COLUMNS = ("account_id", "period", "amount", "currency")

# Bounds. The upload is a human pasting a finance export, not a firehose: a cap that a real monthly
# export will never hit still stops a mistaken 2GB paste from becoming an OOM.
MAX_ROWS = 50_000
MAX_ACCOUNT_ID_LEN = 512

# Int64 is what ClickHouse's ValueAmountMicro holds.
_INT64_MAX = 2**63 - 1

_PERIOD_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PERIOD_DAY_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# Accepts "1234.56", "1,234.56", "$1,234.56", "-1234.56", "(1234.56)" — the shapes a spreadsheet
# export actually produces. Anything else is rejected by line number rather than coerced to zero.
_AMOUNT_STRIP_RE = re.compile(r"[,\s$]")


class RevenueUploadError(ValueError):
    """Caller-facing rejection. The message is safe to return verbatim (HTTP 422)."""

    def __init__(self, message: str, *, errors: list["RowError"] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "detail": str(self),
            "errors": [e.as_dict() for e in self.errors],
        }


@dataclass(frozen=True, slots=True)
class RowError:
    """One rejected row, carrying the line number the operator sees in their editor.

    ``line`` is 1-based over the whole file INCLUDING the header, because that is what a
    spreadsheet or text editor shows. A silently skipped row is the failure mode this replaces:
    revenue that quietly is not there is worse than an upload that refuses.
    """

    line: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "message": self.message}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"line {self.line}: {self.message}"


@dataclass(frozen=True, slots=True)
class RevenueRow:
    """One validated CSV row, before hashing."""

    line: int
    account_id: str
    period: str  # 'YYYY-MM'
    amount_micro: int
    currency: str


@dataclass(frozen=True, slots=True)
class ParsedUpload:
    """A whole accepted file, grouped by the period each row covers."""

    rows: tuple[RevenueRow, ...]

    @property
    def periods(self) -> tuple[str, ...]:
        seen: list[str] = []
        for r in self.rows:
            if r.period not in seen:
                seen.append(r.period)
        return tuple(sorted(seen))

    def rows_for(self, period: str) -> tuple[RevenueRow, ...]:
        return tuple(r for r in self.rows if r.period == period)


def normalize_period(raw: str) -> str | None:
    """``YYYY-MM`` (or a ``YYYY-MM-DD`` a spreadsheet widened it to) → ``YYYY-MM``.

    A month is the granularity finance closes on and the granularity a replace operates at. A day
    inside the month is accepted and narrowed to its month rather than rejected, because
    spreadsheets love to reformat ``2026-08`` into ``2026-08-01`` on their own.
    """
    raw = raw.strip()
    if _PERIOD_MONTH_RE.match(raw):
        return raw
    m = _PERIOD_DAY_RE.match(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _parse_amount(raw: str) -> int | None:
    """Money string → integer micro-units, or None when it is not a number.

    Rounds to the nearest micro-unit. Six decimal places is far finer than any real biller quotes,
    so this only ever fires on a value that was already beyond representable precision.
    """
    text = raw.strip()
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        # Accounting-style negative. Spreadsheets export credits this way.
        negative = True
        text = text[1:-1]
    text = _AMOUNT_STRIP_RE.sub("", text)
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    if negative:
        value = -value
    micro = int((value * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(micro) > _INT64_MAX:
        return None
    return micro


def _header_index(header: list[str]) -> dict[str, int] | RowError:
    """Map required column name → position, or a RowError naming what is missing."""
    seen: dict[str, int] = {}
    for i, cell in enumerate(header):
        # A UTF-8 BOM survives on the first header cell of anything Excel exported, so strip it
        # before matching or `account_id` never matches and every file looks headerless.
        key = cell.strip().lstrip("﻿").strip().lower()
        if key in REQUIRED_COLUMNS and key not in seen:
            seen[key] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in seen]
    if missing:
        return RowError(
            1,
            "header must name the columns "
            + ", ".join(REQUIRED_COLUMNS)
            + f" (missing: {', '.join(missing)})",
        )
    return seen


def parse_revenue_csv(text: str) -> ParsedUpload:
    """Parse and validate a whole revenue CSV. All-or-nothing.

    Raises :class:`RevenueUploadError` carrying EVERY offending line number, so a broken export is
    fixed in one pass rather than one row per attempt. See the module docstring for why a partially
    accepted file is not an option here.
    """
    if not text.strip():
        raise RevenueUploadError("the file is empty")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover - guarded by the emptiness check above
        raise RevenueUploadError("the file is empty") from None

    idx = _header_index(header)
    if isinstance(idx, RowError):
        # Nothing below can be trusted without a header, so this is fatal on its own.
        raise RevenueUploadError(str(idx), errors=[idx])

    errors: list[RowError] = []
    rows: list[RevenueRow] = []
    # (account_id, period) -> line that first claimed it. A duplicate key inside one file would
    # collapse onto one derived BusinessEventId and silently lose an account's revenue, so it is
    # rejected with BOTH line numbers rather than resolved by last-write-wins.
    first_seen: dict[tuple[str, str], int] = {}
    # Currency is pinned per period: summing mixed currencies needs an FX rate we do not have, and
    # inventing one would fabricate the number the whole feature exists to make trustworthy.
    period_currency: dict[str, tuple[str, int]] = {}
    width = max(idx.values()) + 1

    for raw_row in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in raw_row):
            # A trailing blank line is not an error; every export has one.
            continue
        if len(rows) >= MAX_ROWS:
            errors.append(RowError(line, f"more than {MAX_ROWS} data rows — split the file"))
            break
        if len(raw_row) < width:
            errors.append(
                RowError(line, f"expected at least {width} columns, found {len(raw_row)}")
            )
            continue

        account_id = raw_row[idx["account_id"]].strip()
        period_raw = raw_row[idx["period"]]
        amount_raw = raw_row[idx["amount"]]
        currency_raw = raw_row[idx["currency"]].strip().upper()

        row_errors = len(errors)
        if not account_id:
            errors.append(RowError(line, "account_id is empty"))
        elif len(account_id) > MAX_ACCOUNT_ID_LEN:
            errors.append(
                RowError(line, f"account_id is longer than {MAX_ACCOUNT_ID_LEN} characters")
            )

        period = normalize_period(period_raw)
        if period is None:
            errors.append(
                RowError(line, f"period {period_raw.strip()!r} is not a YYYY-MM calendar month")
            )

        amount_micro = _parse_amount(amount_raw)
        if amount_micro is None:
            errors.append(RowError(line, f"amount {amount_raw.strip()!r} is not a number"))

        if not _CURRENCY_RE.match(currency_raw):
            errors.append(
                RowError(line, f"currency {currency_raw!r} is not a 3-letter ISO 4217 code")
            )

        if len(errors) != row_errors:
            continue
        assert period is not None and amount_micro is not None

        key = (account_id, period)
        if key in first_seen:
            errors.append(
                RowError(
                    line,
                    f"account_id {account_id!r} already appears for period {period} on line "
                    f"{first_seen[key]} — one row per account per period",
                )
            )
            continue
        first_seen[key] = line

        pinned = period_currency.get(period)
        if pinned is None:
            period_currency[period] = (currency_raw, line)
        elif pinned[0] != currency_raw:
            errors.append(
                RowError(
                    line,
                    f"currency {currency_raw} does not match {pinned[0]} used for period {period} "
                    f"on line {pinned[1]} — we will not sum currencies without an FX rate",
                )
            )
            continue

        rows.append(
            RevenueRow(
                line=line,
                account_id=account_id,
                period=period,
                amount_micro=amount_micro,
                currency=currency_raw,
            )
        )

    if errors:
        head = "; ".join(str(e) for e in errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        raise RevenueUploadError(
            f"{len(errors)} row(s) rejected, nothing was written: {head}{more}",
            errors=errors,
        )
    if not rows:
        raise RevenueUploadError("the file has a header but no data rows")
    return ParsedUpload(rows=tuple(rows))


# --- ClickHouse row shaping ---------------------------------------------------------------------


def business_event_id(period: str, account_id_hash: str) -> str:
    """Derived, not generated. Same account + same period = same id, upload after upload."""
    return f"{ID_PREFIX}:{period}:{account_id_hash}"


def period_id_prefix(period: str) -> str:
    """Prefix matching every derived id for one period. The scope of a replace's DELETE."""
    return f"{ID_PREFIX}:{period}:"


def period_occurred_at(period: str, *, now: datetime | None = None) -> datetime:
    """Timestamp stamped on a period's rows: the period's last instant, clamped to now.

    A monthly total is not an instant, so something has to be chosen. The period END is the honest
    choice — the total is only complete once the month is — and it keeps a closed month inside the
    dashboard's trailing windows for as long as the window covers the month. Clamping to `now`
    stops a snapshot of the month currently in progress from being stamped in the future, which
    would read as revenue we have not earned yet.
    """
    now = now or datetime.now(tz=UTC)
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=UTC)
    end = next_month - timedelta(seconds=1)
    return min(end, now)


@dataclass(frozen=True, slots=True)
class PeriodSnapshot:
    """One period's worth of an accepted upload, ready to write."""

    period: str
    currency: str
    account_count: int
    total_amount_micro: int
    events: tuple[Any, ...]  # tally.wire.BusinessEvent, kept untyped to avoid a hard import here


def build_period_snapshots(
    parsed: ParsedUpload,
    *,
    hash_account: Any,
    now: datetime | None = None,
) -> tuple[PeriodSnapshot, ...]:
    """Turn an accepted file into per-period :class:`PeriodSnapshot` batches.

    ``hash_account`` maps a plaintext account id to its per-tenant HMAC digest. The plaintext is
    used to compute the hash and is never persisted or logged, exactly as on the Stripe path.

    The account hash is written to BOTH ``AccountIdHash`` and ``UserIdHash``. That is not a
    duplication for convenience: it is what makes uploaded revenue behave *identically* to
    connector-sourced revenue today. ``StripeConnector`` already hashes the Stripe CUSTOMER id into
    ``UserIdHash`` (see plan item E2), so every query that reads revenue reads it out of
    ``UserIdHash`` in that same account-shaped hash space. Writing only ``AccountIdHash`` would make
    uploaded revenue invisible to the shipped margin column; writing only ``UserIdHash`` would leave
    the account dimension CTO-180 added empty. Writing both puts the row in exactly the same place
    every other revenue source puts it, and pre-populates the column E3/E4 will read.
    """
    from tally.wire import BusinessEvent

    occurred_now = now or datetime.now(tz=UTC)
    out: list[PeriodSnapshot] = []
    for period in parsed.periods:
        rows = parsed.rows_for(period)
        occurred_at_ns = int(period_occurred_at(period, now=occurred_now).timestamp() * 1e9)
        events: list[BusinessEvent] = []
        for row in rows:
            digest = hash_account(row.account_id)
            events.append(
                BusinessEvent(
                    business_event_id=business_event_id(period, digest),
                    event_name=UPLOAD_EVENT_NAME,
                    user_id_hash=digest,
                    account_id_hash=digest,
                    occurred_at_ns=occurred_at_ns,
                    value_amount_micro=row.amount_micro,
                    value_currency=row.currency,
                    # A negative period total is a net credit. `refund` is the ValueType the reader
                    # subtracts, so a credit nets off instead of being counted as income or
                    # silently dropped. Positive totals are `monetary`, not `mrr`: this is revenue
                    # recognised in one period, and a tenant who also ingests `mrr` from a biller
                    # would otherwise double count under the CTO-194 include_mrr default.
                    value_type="refund" if row.amount_micro < 0 else "monetary",
                    source=UPLOAD_SOURCE,
                )
            )
        out.append(
            PeriodSnapshot(
                period=period,
                currency=rows[0].currency,
                account_count=len(rows),
                total_amount_micro=sum(r.amount_micro for r in rows),
                events=tuple(events),
            )
        )
    return tuple(out)


# --- Manifest store ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadSnapshotRow:
    """One manifest row: the "as of" fact the dashboard's staleness badge is derived from."""

    period: str
    source: str
    account_count: int
    total_amount_micro: int
    currency: str
    filename: str | None
    uploaded_at: datetime
    uploaded_by: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "source": self.source,
            "account_count": self.account_count,
            "total_amount_micro": self.total_amount_micro,
            "currency": self.currency,
            "filename": self.filename,
            "uploaded_at": self.uploaded_at.isoformat(),
            "uploaded_by": self.uploaded_by,
        }


def _tenant_uuid(cur: Any, tenant_id: str) -> str:
    """Shared name-or-UUID resolution, re-typed so this module raises one error class."""
    try:
        return resolve_tenant_uuid(cur, tenant_id)
    except TenantNotFoundError as exc:
        raise RevenueUploadError(str(exc)) from exc


_SELECT_COLS = """
    period, source, account_count, total_amount_micro, currency, filename, uploaded_at, uploaded_by
"""


def _row_to_snapshot(row: tuple) -> UploadSnapshotRow:
    return UploadSnapshotRow(
        period=str(row[0]),
        source=str(row[1]),
        account_count=int(row[2]),
        total_amount_micro=int(row[3]),
        currency=str(row[4]),
        filename=row[5],
        uploaded_at=row[6],
        uploaded_by=row[7],
    )


class RevenueUploadStore:
    """Postgres surface over ``tenant_revenue_uploads``.

    Deliberately thin: the money lives in ClickHouse, this table only answers "when was this period
    last uploaded, and by whom". Its ``(tenant_id, period)`` primary key is what makes a second
    upload of a period a replacement rather than an accumulation.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[UploadSnapshotRow]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _tenant_uuid(cur, tenant_id)
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM tenant_revenue_uploads "
                "WHERE tenant_id = %s ORDER BY period DESC",
                (resolved,),
            )
            return [_row_to_snapshot(r) for r in cur.fetchall()]

    def record(
        self,
        tenant_id: str,
        snapshot: PeriodSnapshot,
        *,
        filename: str | None,
        uploaded_by: str | None,
    ) -> UploadSnapshotRow:
        """Upsert one period's manifest row. A period can never hold two snapshots."""
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _tenant_uuid(cur, tenant_id)
            cur.execute(
                f"""
                INSERT INTO tenant_revenue_uploads
                    (tenant_id, period, source, account_count, total_amount_micro, currency,
                     filename, uploaded_by, uploaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (tenant_id, period) DO UPDATE SET
                    source = EXCLUDED.source,
                    account_count = EXCLUDED.account_count,
                    total_amount_micro = EXCLUDED.total_amount_micro,
                    currency = EXCLUDED.currency,
                    filename = EXCLUDED.filename,
                    uploaded_by = EXCLUDED.uploaded_by,
                    uploaded_at = now()
                RETURNING {_SELECT_COLS}
                """,
                (
                    resolved,
                    snapshot.period,
                    UPLOAD_SOURCE,
                    snapshot.account_count,
                    snapshot.total_amount_micro,
                    snapshot.currency,
                    filename,
                    uploaded_by,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
            return _row_to_snapshot(row)

    def delete(self, tenant_id: str, period: str) -> bool:
        """Drop one period's manifest row. Returns whether a row was actually removed."""
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _tenant_uuid(cur, tenant_id)
            cur.execute(
                "DELETE FROM tenant_revenue_uploads WHERE tenant_id = %s AND period = %s",
                (resolved, period),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
