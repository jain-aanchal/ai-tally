# SPDX-License-Identifier: Apache-2.0
"""The ``usage_counts`` query behind ``GET /v1/usage`` (CTO-390).

This is the one piece of new production SQL an invoice is computed from, so the things that would
quietly put a wrong number on a bill are pinned here: approximate distinct-counting, an unscoped
tenant, an inclusive period end that bills a span twice, and an empty id counted as a real one.
A fake client, so no ClickHouse is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from gateway.store import ClickHouseStore

MAY = datetime(2026, 5, 1, tzinfo=timezone.utc)
JUNE = datetime(2026, 6, 1, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.result_rows = rows


class FakeClient:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = rows if rows is not None else [(0, 0)]
        self.queries: list[tuple[str, dict[str, object]]] = []

    def query(self, sql: str, parameters: dict[str, object] | None = None) -> FakeResult:
        self.queries.append((sql, parameters or {}))
        return FakeResult(self.rows)


def _store(rows: list[tuple[object, ...]] | None = None) -> tuple[ClickHouseStore, FakeClient]:
    store = ClickHouseStore.__new__(ClickHouseStore)
    client = FakeClient(rows)
    store._client = client  # type: ignore[attr-defined]
    store._settings = None  # type: ignore[attr-defined]
    return store, client


def test_it_returns_the_two_distinct_counts_as_ints() -> None:
    store, _client = _store([(417, 9)])
    assert store.usage_counts("t-acme", period_start=MAY, period_end=JUNE) == (417, 9)


def test_the_counts_are_exact_not_approximate() -> None:
    """``uniq`` is a HyperLogLog estimate. An estimated number on an invoice is one no later query
    reproduces, so the query must use ``uniqExact`` and must not fall back to ``uniq``."""
    store, client = _store()
    store.usage_counts("t-acme", period_start=MAY, period_end=JUNE)
    sql = client.queries[0][0]
    assert "uniqExactIf(" in sql
    assert "uniqIf(" not in sql  # the approximate variant, which would silently round a bill


def test_the_window_is_half_open_so_no_span_is_billed_twice() -> None:
    """``>= start`` and ``< end``. An inclusive end would bill midnight on the first to two periods."""
    store, client = _store()
    store.usage_counts("t-acme", period_start=MAY, period_end=JUNE)
    sql, params = client.queries[0]
    assert "Timestamp >= %(s)s" in sql
    assert "Timestamp < %(e)s" in sql
    assert params["s"] == MAY
    assert params["e"] == JUNE


def test_the_query_is_tenant_scoped_and_passes_the_tenant_as_a_parameter() -> None:
    """Cross-tenant leakage here is a tenant billed for another tenant's traffic."""
    store, client = _store()
    store.usage_counts("t-acme", period_start=MAY, period_end=JUNE)
    sql, params = client.queries[0]
    assert "TenantId = %(t)s" in sql
    assert params["t"] == "t-acme"
    assert "t-acme" not in sql  # parameterised, never interpolated


def test_empty_ids_are_not_counted_as_a_real_trace_or_feature() -> None:
    """An untagged span carries the empty default; counting it invents a feature named "" and bills
    for it."""
    store, client = _store()
    store.usage_counts("t-acme", period_start=MAY, period_end=JUNE)
    sql = client.queries[0][0]
    assert "notEmpty(TraceId)" in sql
    assert "notEmpty(FeatureTag)" in sql


def test_a_period_with_no_spans_reports_zero_rather_than_failing() -> None:
    """A real zero is a real answer here: the tenant sent nothing. It is only "unknown" when the
    source could not be read at all, which is handled a layer up in usage_store."""
    store, _client = _store([(0, 0)])
    assert store.usage_counts("t-acme", period_start=MAY, period_end=JUNE) == (0, 0)
