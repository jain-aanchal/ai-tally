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

from gateway.metering import PlanLimit, UsageRecord
from gateway.usage_store import (
    DurableUsageRollup,
    UsageUnavailable,
    period_bounds,
)

T = "t-acme"
MAY_NS = int(datetime(2026, 5, 15, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


class _FakeCounts:
    """Stands in for ClickHouse: the shared, durable count every replica reads."""

    def __init__(self, traces: int = 0, features: int = 0) -> None:
        self.traces = traces
        self.features = features
        self.calls = 0
        self.unavailable = False

    def usage_counts(
        self, tenant_id: str, *, period_start: datetime, period_end: datetime
    ) -> tuple[int, int]:
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("clickhouse unreachable")
        self.last_window = (period_start, period_end)
        return self.traces, self.features


class _FakeCommitted:
    """Stands in for tenant_usage_periods: the frozen figure for a closed period."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], UsageRecord] = {}
        self.unavailable = False

    def get(self, tenant_id: str, period: str) -> UsageRecord | None:
        if self.unavailable:
            raise UsageUnavailable("postgres unreachable")
        return self.rows.get((tenant_id, period))

    def commit(self, record: UsageRecord) -> None:
        if self.unavailable:
            raise UsageUnavailable("postgres unreachable")
        # First commit wins, matching the real store's ON CONFLICT DO NOTHING.
        self.rows.setdefault((record.tenant_id, record.period), record)


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
) -> DurableUsageRollup:
    return DurableUsageRollup(
        counts_source=lambda: counts,
        committed=committed,
        plan_limits=None if plan is None else (lambda _t: plan),
        cache_ttl_s=ttl,
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
    """THE BUG. Two replicas, one store, one answer. This is what memory could never give."""
    counts = _FakeCounts(traces=417, features=9)
    committed = _FakeCommitted()
    replica_a = _rollup(counts, committed)
    replica_b = _rollup(counts, committed)

    a = replica_a.usage(T, "2026-05")
    b = replica_b.usage(T, "2026-05")

    assert a.trace_count == b.trace_count == 417
    assert a.feature_count == b.feature_count == 9


def test_usage_survives_a_restart() -> None:
    """A redeployed gateway starts with an empty process and must still know the figure."""
    counts = _FakeCounts(traces=52, features=4)
    committed = _FakeCommitted()
    before = _rollup(counts, committed).usage(T, "2026-05")

    restarted = _rollup(counts, committed)  # brand new instance, no carried state
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
