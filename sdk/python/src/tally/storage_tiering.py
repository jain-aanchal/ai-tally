# SPDX-License-Identifier: Apache-2.0
"""Storage tiering & TTL policy (CTO-29 / spec §5.1, Appendix A).

Keeping every raw span on hot SSD forever is the dominant storage cost. We tier by age:

* **hot** SSD: recent spans, fully queried at row granularity.
* **warm** volume: still raw, cheaper disk; powers the last-month deep dives.
* **cold** volume: still raw but object-store-backed; rare late-billing true-ups.
* after the cold horizon the **raw span is dropped** and only the daily rollup aggregate
  (``daily_feature_rollup``, CTO-24) survives, enough for YoY cohorts + reconciliation.

This module is the single source of truth for the tier boundaries. It both *classifies* a span's
tier at query time and *generates* the ClickHouse ``TTL`` DDL, so the table definition and the
runtime logic can never silently drift. Enterprise tenants get longer retention via a per-tenant
override; ClickHouse TTL is table-level, so the override is compiled into a ``multiIf`` expression
keyed on ``TenantId``.

CTO-338 extends the same idea to the tables DERIVED from spans (rollups, attribution, revenue, the
replay corpus), which had no retention at all and had grown larger than the raw table they came
from. Those get delete-only :class:`RetentionPolicy` entries in :data:`DERIVED_TABLE_RETENTION`,
generated and validated here for the same reason the span TTL is: so the DDL and the policy cannot
drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

UTC = timezone.utc

# Canonical default boundaries (days). hot < warm < cold; raw dropped at cold.
DEFAULT_HOT_DAYS = 7
DEFAULT_WARM_DAYS = 30
DEFAULT_COLD_DAYS = 90

# The dimensions the surviving aggregate is keyed by, post raw-drop (spec §5.1). Order is the
# ClickHouse rollup ORDER BY prefix; ``Day`` is the time bucket.
WARM_AGGREGATE_DIMENSIONS = ("TenantId", "FeatureTag", "Day", "GenAiResponseModel")


class StorageTier(str, Enum):
    """Where a span physically lives as a function of its age."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    # Raw row has been dropped by TTL; only the rollup aggregate remains queryable.
    AGGREGATE = "aggregate"


class TtlActionKind(str, Enum):
    MOVE = "move"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class TtlAction:
    """One clause of a ClickHouse ``TTL``: move to a volume, or delete, at ``age_days``."""

    age_days: int
    kind: TtlActionKind
    target: str | None = None  # volume name for MOVE; None for DELETE

    def to_sql(self, *, timestamp_column: str = "Timestamp") -> str:
        base = f"toDateTime({timestamp_column}) + INTERVAL {self.age_days} DAY"
        if self.kind is TtlActionKind.MOVE:
            return f"{base} TO VOLUME '{self.target}'"
        return f"{base} DELETE"


def _check_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class TieringPolicy:
    """Age boundaries + volume names driving both classification and TTL DDL generation."""

    hot_days: int = DEFAULT_HOT_DAYS
    warm_days: int = DEFAULT_WARM_DAYS
    cold_days: int = DEFAULT_COLD_DAYS
    warm_volume: str = "warm"
    cold_volume: str = "cold"

    def __post_init__(self) -> None:
        _check_positive_int(self.hot_days, "hot_days")
        _check_positive_int(self.warm_days, "warm_days")
        _check_positive_int(self.cold_days, "cold_days")
        if not (self.hot_days < self.warm_days < self.cold_days):
            raise ValueError(
                "boundaries must satisfy hot_days < warm_days < cold_days, got "
                f"{self.hot_days} < {self.warm_days} < {self.cold_days}"
            )
        if not self.warm_volume or not self.cold_volume:
            raise ValueError("warm_volume and cold_volume must be non-empty")

    # --- classification ----------------------------------------------------------------------

    def tier_for_age(self, age: timedelta) -> StorageTier:
        """Classify a span by its age. Negative ages (clock skew) are treated as hot."""
        days = age.total_seconds() / 86_400
        if days < self.hot_days:
            return StorageTier.HOT
        if days < self.warm_days:
            return StorageTier.WARM
        if days < self.cold_days:
            return StorageTier.COLD
        return StorageTier.AGGREGATE

    def tier_at(self, span_ts: datetime, as_of: datetime) -> StorageTier:
        """Classify ``span_ts`` as observed at ``as_of`` (both coerced to UTC)."""
        return self.tier_for_age(_as_utc(as_of) - _as_utc(span_ts))

    def raw_dropped(self, span_ts: datetime, as_of: datetime) -> bool:
        """True once the raw span is past the cold horizon (only the aggregate survives)."""
        return self.tier_at(span_ts, as_of) is StorageTier.AGGREGATE

    # --- TTL DDL generation ------------------------------------------------------------------

    def ttl_actions(self) -> tuple[TtlAction, ...]:
        """The ordered TTL transitions: hot→warm, warm→cold, then drop raw at the cold horizon."""
        return (
            TtlAction(self.hot_days, TtlActionKind.MOVE, self.warm_volume),
            TtlAction(self.warm_days, TtlActionKind.MOVE, self.cold_volume),
            TtlAction(self.cold_days, TtlActionKind.DELETE),
        )

    def render_ttl_clause(self, *, timestamp_column: str = "Timestamp") -> str:
        """Render the full multi-line ClickHouse ``TTL`` clause for this policy."""
        clauses = [a.to_sql(timestamp_column=timestamp_column) for a in self.ttl_actions()]
        return "TTL\n    " + ",\n    ".join(clauses)


DEFAULT_POLICY = TieringPolicy()


# --------------------------------------------------------------------------------------------- #
# Per-tenant overrides (enterprise = longer retention)
# --------------------------------------------------------------------------------------------- #
@runtime_checkable
class TieringPolicyStore(Protocol):
    """Resolves the effective tiering policy for a tenant."""

    def policy_for(self, tenant_id: str) -> TieringPolicy: ...


@dataclass(slots=True)
class InMemoryTieringPolicyStore:
    """Default policy with per-tenant overrides (enterprise tenants retain raw spans longer)."""

    default: TieringPolicy = field(default_factory=lambda: DEFAULT_POLICY)
    overrides: dict[str, TieringPolicy] = field(default_factory=dict)

    def policy_for(self, tenant_id: str) -> TieringPolicy:
        return self.overrides.get(tenant_id, self.default)

    def set_override(self, tenant_id: str, policy: TieringPolicy) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        self.overrides[tenant_id] = policy

    def tier_at(self, tenant_id: str, span_ts: datetime, as_of: datetime) -> StorageTier:
        return self.policy_for(tenant_id).tier_at(span_ts, as_of)


def render_tenant_ttl_delete_expression(
    store: InMemoryTieringPolicyStore,
    *,
    timestamp_column: str = "Timestamp",
    tenant_column: str = "TenantId",
) -> str:
    """Compile per-tenant raw-drop horizons into a single ClickHouse ``multiIf`` DELETE expression.

    ClickHouse TTL is table-level, so a per-tenant override can't be a separate clause. Instead the
    delete interval becomes a ``multiIf`` on ``TenantId``: overrides first (deterministic order),
    then the default horizon as the fallback branch.
    """
    return render_tenant_delete_expression(
        store.default.cold_days,
        {t: p.cold_days for t, p in store.overrides.items()},
        timestamp_column=timestamp_column,
        tenant_column=tenant_column,
    )


def render_tenant_delete_expression(
    default_days: int,
    overrides: Mapping[str, int],
    *,
    timestamp_column: str = "Timestamp",
    tenant_column: str = "TenantId",
) -> str:
    """The ``multiIf`` primitive both the raw-span and derived-table overrides compile down to.

    Factored out for CTO-338: the derived tables need exactly the same per-tenant shape as raw
    spans, and a second hand-rolled renderer is how the two would drift apart.
    """
    _check_positive_int(default_days, "default_days")
    if not overrides:
        return TtlAction(default_days, TtlActionKind.DELETE).to_sql(
            timestamp_column=timestamp_column
        )

    branches: list[str] = []
    for tenant_id in sorted(overrides):
        days = overrides[tenant_id]
        _check_positive_int(days, f"overrides[{tenant_id!r}]")
        branches.append(f"{tenant_column} = '{tenant_id}', INTERVAL {days} DAY")
    branches.append(f"INTERVAL {default_days} DAY")
    inner = ", ".join(branches)
    return f"toDateTime({timestamp_column}) + multiIf({inner}) DELETE"


# --------------------------------------------------------------------------------------------- #
# Derived-table retention (CTO-338)
# --------------------------------------------------------------------------------------------- #
# otel_spans was tiered and expired from the start; nothing DERIVED from it was, so on the measured
# local stack the three largest tables in the database were all derived and unmanaged, each of them
# bigger than the raw span table they came from. That inversion is what this policy fixes.
#
# Derived tables get DELETE-only TTLs, not the hot/warm/cold ladder above. The ladder exists to move
# raw spans off expensive SSD while keeping them queryable; these tables are one to three orders of
# magnitude smaller per row of answer and are read at interactive latency, so tiering them would buy
# very little and cost query time. The decision here is only "how long do we keep it".
#
# The horizons are deliberately grouped into four classes rather than picked per table. A number
# that exists only for one table is a number nobody can defend later; a class states a reason that
# outlives the table it was first applied to.

# Class 1: BOOK OF RECORD. Money that cannot be re-derived from anything once raw spans age out.
# Seven years is the ordinary books-and-records horizon for financial history, and these tables are
# the financial history: for any period older than raw retention they are the ONLY record that the
# spend or the revenue ever happened. #325 makes this explicit by carrying such grains through a
# rebuild labelled `not_derivable` rather than recomputing them. They are also tiny, because they
# are aggregates and compress accordingly, so the long horizon is close to free.
BOOK_OF_RECORD_DAYS = 2555

# Class 2: OPERATIONAL GRAIN. A finer-resolution or supporting copy of something class 1 already
# holds. Losing it loses intraday shape or the ability to re-run a stitch, never a dollar figure.
# 13 months so that any month can still be compared against the same month a year earlier, which is
# the longest window a resolution tier is actually asked for.
OPERATIONAL_GRAIN_DAYS = 400

# Class 3: FOLLOWS RAW SPANS. Rows that are pointers into otel_spans, or the raw inbound artifact
# behind a mapped event. They are meaningless once the thing they point at is gone, so they expire
# on the SAME horizon and with the SAME per-tenant overrides as the spans themselves. This is a
# reference to DEFAULT_COLD_DAYS, not a copy of 90, so a per-tenant raw-retention override moves
# both together.
FOLLOWS_RAW_SPANS_DAYS = DEFAULT_COLD_DAYS

# Class 4: OPT-IN REPLAY CORPUS. Not our number to pick. `tenant_replay_config.retention_days`
# (db/postgres/0004_tenant_replay_config.sql, default 30) already states how long captured payloads
# live, and gateway.tenant_replay.CANDIDATE_RESPONSE_RETENTION_CONSENT discloses that horizon to the
# tenant when they opt in. The ClickHouse side simply never enforced it. So this constant mirrors
# that default rather than inventing a second answer, and the per-tenant value belongs in the
# multiIf via render_tenant_delete_expression.
REPLAY_CORPUS_DAYS = 30

# The longest window the dashboard will let anyone ask for (web/lib/explore.ts MAX_WINDOW_DAYS, via
# clampWindowDays). Mirrored here as a floor, not as a target: several revenue and ROI reads take a
# user-selectable range up to this length, and a book-of-record horizon anywhere near it would mean
# a number a customer read last month is quietly smaller this month. The assertion below keeps the
# two apart by a wide margin rather than by luck.
DASHBOARD_MAX_WINDOW_DAYS = 366

# Rollups the operator drift check re-derives from otel_spans (db/clickhouse/checks/rollup_drift.sql
# and the CTO-311 rebuild). If any of these expired BEFORE the raw spans behind them, the check
# would read the missing rollup rows as drift and the rebuild would try to repair a gap that is not
# one. They must all outlive the raw horizon.
SPAN_DERIVED_ROLLUPS = ("daily_feature_rollup", "hourly_feature_rollup", "daily_account_rollup")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Delete-only retention for one derived table (or one column of one).

    ``column`` set means a COLUMN TTL: the row survives and only that column is reset to its
    default. That distinction is the whole point on ``business_events``, where the money must live
    for years and the verbatim inbound payload must not.
    """

    table: str
    timestamp_column: str
    days: int
    rationale: str
    column: str | None = None
    column_type: str | None = None

    def __post_init__(self) -> None:
        _check_positive_int(self.days, f"{self.table} days")
        if not self.table or not self.timestamp_column:
            raise ValueError("table and timestamp_column must be non-empty")
        if not self.rationale:
            raise ValueError(f"{self.table}: every retention number must carry its reason")
        if (self.column is None) != (self.column_type is None):
            raise ValueError(f"{self.table}: column and column_type are set together or not at all")

    def delete_expression(self, overrides: Mapping[str, int] | None = None) -> str:
        return render_tenant_delete_expression(
            self.days, overrides or {}, timestamp_column=self.timestamp_column
        )

    def render_ttl_clause(self, overrides: Mapping[str, int] | None = None) -> str:
        """The clause as it appears in CREATE TABLE (table TTL) or after MODIFY TTL."""
        return f"TTL {self.delete_expression(overrides)}"

    def render_alter(self, overrides: Mapping[str, int] | None = None) -> str:
        """The operator-facing ALTER that applies this policy to an EXISTING populated table.

        A column TTL is applied with MODIFY COLUMN, which is why ``column_type`` has to be carried:
        ClickHouse restates the full type on that form.
        """
        if self.column is not None:
            expr = self.delete_expression(overrides).removesuffix(" DELETE")
            return (
                f"ALTER TABLE {self.table} MODIFY COLUMN {self.column} {self.column_type} "
                f"TTL {expr};"
            )
        return f"ALTER TABLE {self.table} MODIFY TTL {self.delete_expression(overrides)};"


# The policy itself. Ordered longest-lived first so the file reads as the retention ladder it is.
DERIVED_TABLE_RETENTION: tuple[RetentionPolicy, ...] = (
    RetentionPolicy(
        table="daily_feature_rollup",
        timestamp_column="Day",
        days=BOOK_OF_RECORD_DAYS,
        rationale=(
            "the surviving aggregate after raw-drop; for any day older than raw retention it is "
            "the only record that the spend happened at all"
        ),
    ),
    RetentionPolicy(
        table="daily_account_rollup",
        timestamp_column="Day",
        days=BOOK_OF_RECORD_DAYS,
        rationale=(
            "same standing as daily_feature_rollup for the per-customer question; margin per "
            "customer is not answerable for a period whose account rollup is gone"
        ),
    ),
    RetentionPolicy(
        table="business_events",
        timestamp_column="OccurredAt",
        days=BOOK_OF_RECORD_DAYS,
        rationale=(
            "inbound customer revenue, not derived from anything we hold; shortening this would "
            "silently shrink a revenue figure a customer already read off the dashboard"
        ),
    ),
    RetentionPolicy(
        table="attribution_records",
        timestamp_column="AttributedTraceTs",
        days=BOOK_OF_RECORD_DAYS,
        rationale=(
            "the ROI join, and it must expire with business_events rather than before it: revenue "
            "whose attribution has aged out reads as unattributed and flips a reported margin"
        ),
    ),
    RetentionPolicy(
        table="hourly_feature_rollup",
        timestamp_column="Hour",
        days=OPERATIONAL_GRAIN_DAYS,
        rationale=(
            "a resolution tier over money daily_feature_rollup already holds; expiring it loses "
            "intraday shape for old periods, never a total"
        ),
    ),
    RetentionPolicy(
        table="identity_graph",
        timestamp_column="ObservedAt",
        days=OPERATIONAL_GRAIN_DAYS,
        rationale=(
            "stitching substrate, and hashed personal data; once attribution_records is written "
            "the edge has done its job, so the shorter horizon is also the privacy-preferable one"
        ),
    ),
    RetentionPolicy(
        table="unattributed_events",
        timestamp_column="OccurredAt",
        days=OPERATIONAL_GRAIN_DAYS,
        rationale=(
            "the reconciler's re-check queue; an event still unattributed after 13 months is not "
            "going to become attributed, and the revenue itself lives on in business_events"
        ),
    ),
    RetentionPolicy(
        table="eval_runs",
        timestamp_column="JudgedAt",
        days=OPERATIONAL_GRAIN_DAYS,
        rationale=(
            "judge verdicts carry no bodies and are the record of a quality claim we published, "
            "so they outlive the corpus they graded rather than expiring with it"
        ),
    ),
    RetentionPolicy(
        table="last_touch_index",
        timestamp_column="UpdatedAt",
        days=FOLLOWS_RAW_SPANS_DAYS,
        rationale=(
            "every row is a pointer at one otel_spans row; when the span is dropped the pointer "
            "dangles, so it expires on the raw-span horizon and under the same per-tenant override"
        ),
    ),
    RetentionPolicy(
        table="business_events",
        column="RawPayload",
        column_type="String CODEC(ZSTD(3))",
        timestamp_column="OccurredAt",
        days=FOLLOWS_RAW_SPANS_DAYS,
        rationale=(
            "the verbatim inbound webhook body is the raw artifact behind the mapped event, the "
            "same role a span plays, and it is what makes this table large; the money stays for "
            "seven years and only the payload behind it expires on the raw horizon"
        ),
    ),
    RetentionPolicy(
        table="replay_samples",
        timestamp_column="CapturedAt",
        days=REPLAY_CORPUS_DAYS,
        rationale=(
            "tenant_replay_config.retention_days already promises this horizon in the opt-in "
            "consent text; ClickHouse just never enforced it"
        ),
    ),
    RetentionPolicy(
        table="replay_runs",
        timestamp_column="RanAt",
        days=REPLAY_CORPUS_DAYS,
        rationale=(
            "holds ResponseText, a real candidate-model body under the CTO-125 PII carve-out, so "
            "it cannot outlive the retention_days the tenant consented to"
        ),
    ),
)


def retention_for(table: str, *, column: str | None = None) -> RetentionPolicy:
    """Look up one policy. Raises rather than returning a default: an unmanaged table is a bug."""
    for policy in DERIVED_TABLE_RETENTION:
        if policy.table == table and policy.column == column:
            return policy
    raise KeyError(f"no retention policy for {table}" + (f".{column}" if column else ""))


def _validate_retention_ladder() -> None:
    """The relationships between the numbers, asserted at import rather than left to a comment.

    These are the constraints that make the policy coherent. If someone shortens one horizon in
    isolation, this is what stops it: a book-of-record table that no longer outlives raw spans has
    stopped being a book of record, and attribution that expires before the revenue it explains
    turns paid-for spend into unattributed spend on a chart nobody re-checked.
    """
    if not (REPLAY_CORPUS_DAYS < OPERATIONAL_GRAIN_DAYS < BOOK_OF_RECORD_DAYS):
        raise ValueError("retention classes must stay ordered: replay < operational < book")
    for policy in DERIVED_TABLE_RETENTION:
        if policy.days == BOOK_OF_RECORD_DAYS and policy.days <= DEFAULT_POLICY.cold_days:
            raise ValueError(f"{policy.table} must outlive the raw span horizon")
    if retention_for("attribution_records").days != retention_for("business_events").days:
        raise ValueError("attribution_records and business_events must expire together")
    if retention_for("last_touch_index").days != DEFAULT_POLICY.cold_days:
        raise ValueError("last_touch_index must track the raw span horizon exactly")
    for table in SPAN_DERIVED_ROLLUPS:
        if retention_for(table).days <= DEFAULT_POLICY.cold_days:
            raise ValueError(f"{table} must outlive raw spans or rollup_drift.sql misreads it")
    # Revenue and the ROI join must outlast the longest window anyone can ASK for, by enough that
    # the horizon is never the thing that moved a figure. Reads over these tables today include
    # genuinely unbounded ones (the all-time attribution-rate KPI), and their empty-result branches
    # are not all honest: an empty business_events currently renders attribution rate as 100%. Until
    # those readers can tell "expired" from "never happened", the only safe horizon is one no read
    # reaches. 7 years against a 366-day ceiling is that.
    for table in ("business_events", "attribution_records"):
        if retention_for(table).days < DASHBOARD_MAX_WINDOW_DAYS * 4:
            raise ValueError(
                f"{table} must outlast the dashboard's longest window by a wide margin"
            )
    if retention_for("replay_runs").days > retention_for("replay_samples").days:
        raise ValueError("replay_runs holds bodies and must not outlive the corpus it graded")


_validate_retention_ladder()


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC; treat naive datetimes as already-UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
