# SPDX-License-Identifier: Apache-2.0
"""Durable, shared billing usage (CTO-390).

The bug these prove fixed: usage was counted into a dict inside one gateway process. With more than
one replica every answer was a fraction of actual usage, two reads of the same dashboard disagreed,
and a deploy reset the count to zero. The load-bearing assertions here are therefore "a second,
independent instance sees the same totals" and "an unavailable source produces an error rather than
a small number", not the arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.config import Settings
from gateway.metering import PlanLimit, UsageRecord
from gateway.usage_store import (
    CommittedUsageStore,
    DurableUsageRollup,
    UsageUnavailable,
    build_usage_rollup,
    period_bounds,
)

T = "t-acme"
MAY_NS = int(datetime(2026, 5, 15, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


class _SharedSpans:
    """The one shared ``otel_spans`` every replica counts over. Not a per-replica copy.

    Split from the client deliberately (CTO-390 review): handing two "replicas" the SAME fake object
    would pass for any implementation that reads whatever it was injected with, which is the very
    property the durability and sharing tests are supposed to prove. A replica gets its own CLIENT;
    the table behind the clients is this.
    """

    def __init__(self, traces: int = 0, features: int = 0) -> None:
        self.traces = traces
        self.features = features
        self.queries = 0


class _SharedPeriods:
    """The one shared ``tenant_usage_periods``, likewise behind per-replica clients."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], UsageRecord] = {}


class _FakeCounts:
    """ONE replica's client over :class:`_SharedSpans`. Two replicas mean two of these, one table."""

    def __init__(
        self, traces: int = 0, features: int = 0, *, shared: _SharedSpans | None = None
    ) -> None:
        self.shared = shared if shared is not None else _SharedSpans(traces, features)
        self.calls = 0
        self.unavailable = False

    @property
    def traces(self) -> int:
        return self.shared.traces

    @traces.setter
    def traces(self, value: int) -> None:
        self.shared.traces = value

    @property
    def features(self) -> int:
        return self.shared.features

    @features.setter
    def features(self, value: int) -> None:
        self.shared.features = value

    def usage_counts(
        self, tenant_id: str, *, period_start: datetime, period_end: datetime
    ) -> tuple[int, int]:
        self.calls += 1
        self.shared.queries += 1
        if self.unavailable:
            raise RuntimeError("clickhouse unreachable")
        self.last_window = (period_start, period_end)
        return self.shared.traces, self.shared.features


class _FakeCommitted:
    """ONE replica's client over :class:`_SharedPeriods`: the frozen figure for a closed period."""

    def __init__(self, *, shared: _SharedPeriods | None = None) -> None:
        self.shared = shared if shared is not None else _SharedPeriods()
        self.unavailable = False

    @property
    def rows(self) -> dict[tuple[str, str], UsageRecord]:
        return self.shared.rows

    def get(self, tenant_id: str, period: str) -> UsageRecord | None:
        if self.unavailable:
            raise UsageUnavailable("postgres unreachable")
        return self.shared.rows.get((tenant_id, period))

    def commit(self, record: UsageRecord) -> None:
        if self.unavailable:
            raise UsageUnavailable("postgres unreachable")
        # First commit wins, matching the real store's ON CONFLICT DO NOTHING.
        self.shared.rows.setdefault((record.tenant_id, record.period), record)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _rollup(
    counts: _FakeCounts,
    committed: _FakeCommitted | None = None,
    *,
    ttl: float = 15.0,
    clock: _Clock | None = None,
    plan: PlanLimit | None = None,
    retention_days: int = 90,
) -> DurableUsageRollup:
    return DurableUsageRollup(
        counts_source=lambda: counts,
        committed=committed,
        plan_limits=None if plan is None else (lambda _t: plan),
        cache_ttl_s=ttl,
        live_count_retention_days=retention_days,
        now_s=clock or _Clock(),
        now_ns=lambda: MAY_NS,
    )


# --- period bounds --------------------------------------------------------------------------------


def test_period_bounds_is_half_open_utc() -> None:
    start, end = period_bounds("2026-05")
    assert start == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_period_bounds_rolls_the_year_at_december() -> None:
    assert period_bounds("2026-12")[1] == datetime(2027, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["2026", "2026-13", "2026-00", "26-05", "not-a-period", ""])
def test_a_malformed_period_is_refused(bad: str) -> None:
    """A loosely parsed period would bill a window nobody asked about."""
    with pytest.raises(ValueError):
        period_bounds(bad)


# --- the durable read path ------------------------------------------------------------------------


def test_usage_reads_the_shared_count_not_an_in_process_one() -> None:
    counts = _FakeCounts(traces=3, features=2)
    usage = _rollup(counts).usage(T, "2026-05")
    assert usage.trace_count == 3
    assert usage.feature_count == 2
    assert usage.closed is False


def test_two_independent_instances_sharing_one_store_report_the_same_totals() -> None:
    """THE BUG. Two replicas, one store, one answer. This is what memory could never give.

    The replicas get SEPARATE client objects over one shared table, never the same object: sharing
    one fake between them would pass for any implementation that simply reads what it was handed,
    which is exactly the property under test. ``shared.queries`` then proves both replicas really
    went to the shared table rather than one of them answering from something it kept.
    """
    spans = _SharedSpans(traces=417, features=9)
    periods = _SharedPeriods()
    replica_a = _rollup(_FakeCounts(shared=spans), _FakeCommitted(shared=periods))
    replica_b = _rollup(_FakeCounts(shared=spans), _FakeCommitted(shared=periods))

    a = replica_a.usage(T, "2026-05")
    b = replica_b.usage(T, "2026-05")

    assert a.trace_count == b.trace_count == 417
    assert a.feature_count == b.feature_count == 9
    assert spans.queries == 2  # each replica read the shared table for itself


def test_a_figure_committed_by_one_replica_is_the_figure_the_other_serves() -> None:
    """Sharing through the committed store too, not just the live count.

    A closing job runs on whichever replica wins the lock, and every other replica has to serve what
    it wrote. Separate client objects again, so this cannot pass by accident.
    """
    spans = _SharedSpans(traces=2, features=1)
    periods = _SharedPeriods()
    writer = _FakeCommitted(shared=periods)
    writer.commit(
        UsageRecord(
            tenant_id=T, period="2026-05", trace_count=880, feature_count=6,
            trace_commitment="c", feature_commitment="d", plan="free",
            trace_limit=None, feature_limit=None, closed=True,
        )
    )

    reader = _rollup(_FakeCounts(shared=spans), _FakeCommitted(shared=periods))
    served = reader.usage(T, "2026-05")

    assert served.trace_count == 880
    assert served.closed is True
    assert spans.queries == 0  # the committed row won, so the live count was never consulted


def test_usage_survives_a_restart() -> None:
    """A redeployed gateway starts with an empty process and must still know the figure.

    The restart mints BRAND NEW client objects over the same shared table, which is what a redeploy
    actually is. Re-using the same fakes would only prove the rollup reads its own injected source.
    """
    spans = _SharedSpans(traces=52, features=4)
    periods = _SharedPeriods()
    before = _rollup(_FakeCounts(shared=spans), _FakeCommitted(shared=periods)).usage(T, "2026-05")

    # New process: new rollup, new clients, nothing carried across but the store itself.
    restarted = _rollup(_FakeCounts(shared=spans), _FakeCommitted(shared=periods))
    after = restarted.usage(T, "2026-05")

    assert before.trace_count == after.trace_count == 52
    assert after.feature_count == 4


def test_the_live_count_reports_its_commitment_as_unknown_not_as_empty() -> None:
    """Honest under uncertainty: a commitment we did not compute is null, never a placeholder."""
    usage = _rollup(_FakeCounts(traces=1, features=1)).usage(T, "2026-05")
    assert usage.trace_commitment is None
    assert usage.feature_commitment is None
    assert usage.as_dict()["trace_commitment"] is None


def test_plan_limits_still_come_from_configuration() -> None:
    counts = _FakeCounts(traces=5, features=3)
    usage = _rollup(counts, plan=PlanLimit(plan="starter", trace_limit=2, feature_limit=1)).usage(
        T, "2026-05"
    )
    assert usage.plan == "starter"
    assert usage.over_trace_limit is True
    assert usage.over_feature_limit is True


# --- unavailability: an error, never a small number -----------------------------------------------


def test_an_unavailable_count_source_raises_rather_than_reporting_zero() -> None:
    """A zero here is indistinguishable from a real zero, which is how a tenant gets a wrong bill."""
    counts = _FakeCounts(traces=100, features=5)
    counts.unavailable = True
    with pytest.raises(UsageUnavailable):
        _rollup(counts).usage(T, "2026-05")


def test_an_unavailable_committed_store_raises_even_though_the_live_count_would_answer() -> None:
    """Without the committed table we cannot tell whether this period is frozen, so we must not guess."""
    counts = _FakeCounts(traces=100, features=5)
    committed = _FakeCommitted()
    committed.unavailable = True
    with pytest.raises(UsageUnavailable):
        _rollup(counts, committed).usage(T, "2026-05")


def test_an_unavailable_source_is_not_answered_from_a_stale_cache_entry() -> None:
    counts = _FakeCounts(traces=7, features=1)
    clock = _Clock()
    rollup = _rollup(counts, ttl=15.0, clock=clock)
    assert rollup.usage(T, "2026-05").trace_count == 7

    clock.t = 100.0  # past the TTL
    counts.unavailable = True
    with pytest.raises(UsageUnavailable):
        rollup.usage(T, "2026-05")


# --- committed periods ----------------------------------------------------------------------------


def test_a_committed_period_wins_over_the_live_count() -> None:
    """A billed month must not move when a late backfill lands or the TTL ages spans out."""
    counts = _FakeCounts(traces=2, features=1)  # what ClickHouse still holds today
    committed = _FakeCommitted()
    committed.commit(
        UsageRecord(
            tenant_id=T,
            period="2026-05",
            trace_count=900,
            feature_count=7,
            trace_commitment="abc",
            feature_commitment="def",
            plan="free",
            trace_limit=None,
            feature_limit=None,
            closed=True,
        )
    )

    usage = _rollup(counts, committed).usage(T, "2026-05")

    assert usage.trace_count == 900  # the frozen figure, not the 2 still in ClickHouse
    assert usage.closed is True
    assert usage.trace_commitment == "abc"
    assert counts.calls == 0  # the live count is not even consulted


def test_committing_twice_keeps_the_first_figure() -> None:
    """Immutability (CTO-86): re-running a closing job must not rewrite a billed month."""
    committed = _FakeCommitted()
    first = UsageRecord(
        tenant_id=T, period="2026-05", trace_count=10, feature_count=1,
        trace_commitment="a", feature_commitment="b", plan="free",
        trace_limit=None, feature_limit=None, closed=True,
    )
    committed.commit(first)
    committed.commit(
        UsageRecord(
            tenant_id=T, period="2026-05", trace_count=999, feature_count=9,
            trace_commitment="x", feature_commitment="y", plan="free",
            trace_limit=None, feature_limit=None, closed=True,
        )
    )
    assert committed.get(T, "2026-05").trace_count == 10


# --- the cache is a cache -------------------------------------------------------------------------


def test_repeat_reads_inside_the_ttl_do_not_re_query() -> None:
    counts = _FakeCounts(traces=4, features=2)
    clock = _Clock()
    rollup = _rollup(counts, ttl=15.0, clock=clock)
    rollup.usage(T, "2026-05")
    rollup.usage(T, "2026-05")
    assert counts.calls == 1


def test_the_cache_refreshes_after_its_ttl() -> None:
    counts = _FakeCounts(traces=4, features=2)
    clock = _Clock()
    rollup = _rollup(counts, ttl=15.0, clock=clock)
    assert rollup.usage(T, "2026-05").trace_count == 4

    counts.traces = 11
    clock.t = 16.0
    assert rollup.usage(T, "2026-05").trace_count == 11
    assert counts.calls == 2


# --- the raw-span retention horizon ---------------------------------------------------------------


def test_a_period_past_the_retention_horizon_is_unknown_not_a_decayed_count() -> None:
    """``otel_spans`` DELETEs raw rows at 90 days with no aggregate-on-expire, and nothing commits a
    period on a schedule yet. A live count over an old month therefore returns whatever rows still
    survive: a real figure shrinking a little every day, served as ordinary open data. Refusing is
    the honest alternative until the closing job exists."""
    counts = _FakeCounts(traces=3, features=1)  # the remnants ClickHouse still holds
    with pytest.raises(UsageUnavailable):
        _rollup(counts, _FakeCommitted()).usage(T, "2025-01")
    assert counts.calls == 0  # refused before asking, rather than after getting a small number


def test_a_period_inside_the_retention_horizon_is_still_counted_live() -> None:
    """The guard must not swallow the current month or a recent one."""
    assert _rollup(_FakeCounts(traces=12, features=2), _FakeCommitted()).usage(
        T, "2026-04"
    ).trace_count == 12


def test_a_committed_period_is_served_however_old_it_is() -> None:
    """The frozen row is the whole reason the retention horizon is survivable at all."""
    committed = _FakeCommitted()
    committed.commit(
        UsageRecord(
            tenant_id=T, period="2025-01", trace_count=4321, feature_count=8,
            trace_commitment="c", feature_commitment="d", plan="free",
            trace_limit=None, feature_limit=None, closed=True,
        )
    )
    usage = _rollup(_FakeCounts(traces=0, features=0), committed).usage(T, "2025-01")
    assert usage.trace_count == 4321
    assert usage.closed is True


# --- the boot probe -------------------------------------------------------------------------------

_UNREACHABLE = "postgresql://nobody@127.0.0.1:1/nowhere"


def test_the_boot_probe_records_why_the_committed_table_is_unusable() -> None:
    """A missing migration 0034 must be visible at boot and nameable in the 503, not discovered as
    an undifferentiated error with the cause only in a startup log nobody is reading."""
    rollup = build_usage_rollup(
        Settings(postgres_dsn=_UNREACHABLE),
        counts_source=lambda: _FakeCounts(traces=1, features=1),
    )
    assert rollup.boot_warning  # a real reason string, not merely a flag


def test_usage_durable_required_turns_a_missing_table_into_a_startup_failure() -> None:
    """The same escape hatch idempotency_durable_required gives, and for the same reason: a replica
    that booted during a Postgres blip would otherwise serve errors and nobody would know why."""
    with pytest.raises(RuntimeError, match="0034"):
        build_usage_rollup(
            Settings(postgres_dsn=_UNREACHABLE, usage_durable_required=True),
            counts_source=lambda: _FakeCounts(traces=1, features=1),
        )


def test_a_failed_probe_still_leaves_the_committed_store_attached() -> None:
    """Detaching it would leave the read path unable to tell a frozen period from an open one, so it
    would answer live counts for months that have already been invoiced. An error is better."""
    rollup = build_usage_rollup(
        Settings(postgres_dsn=_UNREACHABLE),
        counts_source=lambda: _FakeCounts(traces=5, features=1),
    )
    with pytest.raises(UsageUnavailable):
        rollup.usage(T, "2026-05")


# --- a floor must not be frozen as if it were the figure (CTO-401 review) --------------------------


def _record(*, trace_exact: bool = True, feature_exact: bool = True) -> UsageRecord:
    return UsageRecord(
        tenant_id=T, period="2026-05", trace_count=250_000, feature_count=7,
        trace_commitment=None, feature_commitment=None, plan="free",
        trace_limit=None, feature_limit=None, closed=True,
        trace_count_exact=trace_exact, feature_count_exact=feature_exact,
    )


def test_committing_a_saturated_period_is_refused() -> None:
    """tenant_usage_periods has no exactness column, so an inexact row reads back as exact forever.

    The table is immutable by design, which is exactly what makes this unrecoverable: a floor stored
    as a plain number becomes a permanent, confident figure that nobody can later tell apart from a
    real one, in a row that cannot be corrected. Refusing keeps the schema honest without adding a
    column for a record the durable path (an exact ClickHouse uniqExact) never produces.
    """
    store = CommittedUsageStore(Settings(postgres_dsn=_UNREACHABLE))
    with pytest.raises(ValueError, match="floor"):
        store.commit(_record(trace_exact=False))
    with pytest.raises(ValueError, match="floor"):
        store.commit(_record(feature_exact=False))


def test_an_exact_record_passes_the_guard_and_reaches_postgres() -> None:
    """The guard rejects the inexact case only. An exact record gets as far as the connection,
    which is what proves the refusal above is about exactness and not about refusing everything."""
    store = CommittedUsageStore(Settings(postgres_dsn=_UNREACHABLE))
    with pytest.raises(UsageUnavailable):
        store.commit(_record())
