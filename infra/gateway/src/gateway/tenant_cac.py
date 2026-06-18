"""Per-tenant monthly CAC inputs for the unit-economics view (CTO-111).

CAC is a *tenant-scoped monthly aggregate* — finance edits one row per month, locks it when the
period closes, ~12 rows per tenant per year steady-state. That shape lives in Postgres next to the
other control-plane state (tenant_connectors, tenant_replay_config); it emphatically does not
belong in ClickHouse.

Reads/writes go through the gateway's ``/v1/tenant/cac`` endpoints — the web app never touches
Postgres directly, same pattern as :mod:`gateway.tenant_replay`.

Locking rule: a period is editable until the *next* period exists. The upsert path refuses to
mutate a row whose ``closed_at`` is set; closing the prior period happens implicitly when the
successor month is inserted. The frontend grays out the form for the same months — backend is the
authoritative check.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import psycopg

from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class CacPeriod:
    period_start: date
    period_end: date
    currency: str
    paid_spend_micro_usd: int
    sales_spend_micro_usd: int
    content_spend_micro_usd: int
    overhead_micro_usd: int
    new_customers_paid: int
    new_customers_total: int
    notes: str | None
    closed_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "paid_spend_micro_usd": self.paid_spend_micro_usd,
            "sales_spend_micro_usd": self.sales_spend_micro_usd,
            "content_spend_micro_usd": self.content_spend_micro_usd,
            "overhead_micro_usd": self.overhead_micro_usd,
            "new_customers_paid": self.new_customers_paid,
            "new_customers_total": self.new_customers_total,
            "notes": self.notes,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "locked": self.closed_at is not None,
        }


CSV_COLUMNS = (
    "period_start", "paid", "sales", "content", "overhead",
    "customers_paid", "customers_total", "notes",
)


class CacPeriodError(ValueError):
    """Caller-facing validation error — surfaces as HTTP 422 in the gateway."""


def _parse_period_start(s: str) -> date:
    s = s.strip()
    try:
        if len(s) == 7:
            return datetime.strptime(s, "%Y-%m").date().replace(day=1)
        return datetime.strptime(s, "%Y-%m-%d").date().replace(day=1)
    except ValueError as exc:
        raise CacPeriodError(
            f"period_start must be YYYY-MM or YYYY-MM-DD, got {s!r}"
        ) from exc


def _end_of_month(start: date) -> date:
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1, day=1)
    else:
        next_month = start.replace(month=start.month + 1, day=1)
    return next_month - timedelta(days=1)


@dataclass(frozen=True, slots=True)
class CacFormInput:
    period_start: date
    paid_spend_micro_usd: int
    sales_spend_micro_usd: int
    content_spend_micro_usd: int
    overhead_micro_usd: int
    new_customers_paid: int
    new_customers_total: int
    notes: str | None

    @classmethod
    def from_json(cls, body: dict) -> "CacFormInput":
        if not isinstance(body, dict):
            raise CacPeriodError("body must be a JSON object")
        ps_raw = body.get("period_start")
        if not isinstance(ps_raw, str):
            raise CacPeriodError("period_start is required (YYYY-MM-DD)")
        return cls(
            period_start=_parse_period_start(ps_raw),
            paid_spend_micro_usd=_as_micro(body.get("paid_spend_micro_usd", 0), "paid_spend"),
            sales_spend_micro_usd=_as_micro(body.get("sales_spend_micro_usd", 0), "sales_spend"),
            content_spend_micro_usd=_as_micro(body.get("content_spend_micro_usd", 0), "content_spend"),
            overhead_micro_usd=_as_micro(body.get("overhead_micro_usd", 0), "overhead"),
            new_customers_paid=_as_count(body.get("new_customers_paid", 0), "new_customers_paid"),
            new_customers_total=_as_count(body.get("new_customers_total", 0), "new_customers_total"),
            notes=_as_notes(body.get("notes")),
        )

    def sanity_check(self) -> None:
        if self.new_customers_total < self.new_customers_paid:
            raise CacPeriodError(
                "new_customers_total must be >= new_customers_paid "
                f"(got total={self.new_customers_total}, paid={self.new_customers_paid})"
            )


def _as_micro(v: object, field: str) -> int:
    if isinstance(v, bool):
        raise CacPeriodError(f"{field} must be a number")
    if isinstance(v, int):
        result = v
    elif isinstance(v, float):
        result = int(round(v))
    elif isinstance(v, str):
        try:
            result = int(round(float(v)))
        except ValueError as exc:
            raise CacPeriodError(f"{field} must be a number, got {v!r}") from exc
    else:
        raise CacPeriodError(f"{field} must be a number")
    if result < 0:
        raise CacPeriodError(f"{field} must be >= 0")
    return result


def _as_count(v: object, field: str) -> int:
    if isinstance(v, bool):
        raise CacPeriodError(f"{field} must be an integer")
    if isinstance(v, int):
        result = v
    elif isinstance(v, str):
        try:
            result = int(v)
        except ValueError as exc:
            raise CacPeriodError(f"{field} must be an integer, got {v!r}") from exc
    elif isinstance(v, float) and v.is_integer():
        result = int(v)
    else:
        raise CacPeriodError(f"{field} must be an integer")
    if result < 0:
        raise CacPeriodError(f"{field} must be >= 0")
    return result


def _as_notes(v: object) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise CacPeriodError("notes must be a string")
    return v


def parse_csv(body: str) -> list[CacFormInput]:
    """Parse a fixed-column-order CSV upload into validated rows."""
    reader = csv.reader(io.StringIO(body))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CacPeriodError("CSV is empty") from exc
    header_clean = [h.strip() for h in header]
    if tuple(header_clean) != CSV_COLUMNS:
        raise CacPeriodError(
            f"CSV header must be exactly {','.join(CSV_COLUMNS)}, "
            f"got {','.join(header_clean)}"
        )
    rows: list[CacFormInput] = []
    for i, raw in enumerate(reader, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        if len(raw) != len(CSV_COLUMNS):
            raise CacPeriodError(
                f"row {i}: expected {len(CSV_COLUMNS)} columns, got {len(raw)}"
            )
        try:
            form = CacFormInput.from_json({
                "period_start": raw[0],
                "paid_spend_micro_usd": raw[1] or 0,
                "sales_spend_micro_usd": raw[2] or 0,
                "content_spend_micro_usd": raw[3] or 0,
                "overhead_micro_usd": raw[4] or 0,
                "new_customers_paid": raw[5] or 0,
                "new_customers_total": raw[6] or 0,
                "notes": raw[7],
            })
            # Run sanity guard at parse time so the line-number-prefixed error surfaces in the
            # CSV-upload error path (the upsert path raises bare, losing row context).
            form.sanity_check()
            rows.append(form)
        except CacPeriodError as exc:
            raise CacPeriodError(f"row {i}: {exc}") from exc
    return rows


def csv_template() -> str:
    """Downloadable template — header row + one example row finance can fill in."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    w.writerow(["2026-01-01", "5000000000", "8000000000", "2000000000",
                "3000000000", "40", "120", "Q1 push"])
    return buf.getvalue()


class TenantCacStore:
    """Postgres surface over ``cac_periods``."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[CacPeriod]:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT period_start, period_end, currency,
                       paid_spend_micro_usd, sales_spend_micro_usd, content_spend_micro_usd,
                       overhead_micro_usd, new_customers_paid, new_customers_total,
                       notes, closed_at
                FROM cac_periods
                WHERE tenant_id = %s
                ORDER BY period_start DESC
                """,
                (tenant_id,),
            )
            return [_row_to_period(r) for r in cur.fetchall()]

    def upsert(self, tenant_id: str, form: CacFormInput) -> CacPeriod:
        form.sanity_check()
        period_end = _end_of_month(form.period_start)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM cac_periods WHERE tenant_id = %s AND period_start = %s",
                (tenant_id, form.period_start),
            )
            existing = cur.fetchone()
            if existing and existing[0] is not None:
                raise CacPeriodError(
                    f"period {form.period_start.isoformat()} is locked (closed_at is set)"
                )
            cur.execute(
                """
                INSERT INTO cac_periods (
                    tenant_id, period_start, period_end, currency,
                    paid_spend_micro_usd, sales_spend_micro_usd, content_spend_micro_usd,
                    overhead_micro_usd, new_customers_paid, new_customers_total,
                    notes, updated_at
                ) VALUES (%s,%s,%s,'USD',%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (tenant_id, period_start) DO UPDATE
                  SET period_end              = EXCLUDED.period_end,
                      paid_spend_micro_usd    = EXCLUDED.paid_spend_micro_usd,
                      sales_spend_micro_usd   = EXCLUDED.sales_spend_micro_usd,
                      content_spend_micro_usd = EXCLUDED.content_spend_micro_usd,
                      overhead_micro_usd      = EXCLUDED.overhead_micro_usd,
                      new_customers_paid      = EXCLUDED.new_customers_paid,
                      new_customers_total     = EXCLUDED.new_customers_total,
                      notes                   = EXCLUDED.notes,
                      updated_at              = now()
                RETURNING period_start, period_end, currency,
                          paid_spend_micro_usd, sales_spend_micro_usd, content_spend_micro_usd,
                          overhead_micro_usd, new_customers_paid, new_customers_total,
                          notes, closed_at
                """,
                (tenant_id, form.period_start, period_end,
                 form.paid_spend_micro_usd, form.sales_spend_micro_usd,
                 form.content_spend_micro_usd, form.overhead_micro_usd,
                 form.new_customers_paid, form.new_customers_total, form.notes),
            )
            row = cur.fetchone()
            cur.execute(
                """
                UPDATE cac_periods SET closed_at = now()
                 WHERE tenant_id = %s AND period_start < %s AND closed_at IS NULL
                """,
                (tenant_id, form.period_start),
            )
            conn.commit()
            return _row_to_period(row)

    def upsert_many(self, tenant_id: str, forms: Iterable[CacFormInput]) -> list[CacPeriod]:
        return [self.upsert(tenant_id, f) for f in forms]


def _row_to_period(row: tuple) -> CacPeriod:
    return CacPeriod(
        period_start=row[0], period_end=row[1], currency=row[2],
        paid_spend_micro_usd=int(row[3]),
        sales_spend_micro_usd=int(row[4]),
        content_spend_micro_usd=int(row[5]),
        overhead_micro_usd=int(row[6]),
        new_customers_paid=int(row[7]),
        new_customers_total=int(row[8]),
        notes=row[9],
        closed_at=_as_aware(row[10]),
    )


def _as_aware(v):  # pragma: no cover
    if v is None:
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v
