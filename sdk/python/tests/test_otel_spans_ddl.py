"""Structural validation of the otel_spans DDL (CTO-22).

We can't stand up ClickHouse in CI here, so this asserts the invariants the spec makes
load-bearing: TenantId-first ORDER BY, Decimal64 (not Float) cost, dual-track + key-version
columns, required codecs, and the bloom-filter indexes. A guard against silent schema drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DDL_PATH = Path(__file__).resolve().parents[3] / "db" / "clickhouse" / "otel_spans.sql"


@pytest.fixture(scope="module")
def ddl() -> str:
    assert DDL_PATH.exists(), f"DDL not found at {DDL_PATH}"
    return DDL_PATH.read_text()


@pytest.fixture(scope="module")
def ddl_code(ddl: str) -> str:
    """DDL with ``--`` comment lines stripped, so assertions test executable SQL only."""
    return "\n".join(line for line in ddl.splitlines() if not line.lstrip().startswith("--"))


def test_table_created(ddl):
    assert "CREATE TABLE IF NOT EXISTS otel_spans" in ddl


def test_tenant_id_first_in_order_by(ddl):
    m = re.search(r"ORDER BY \(([^)]*)\)", ddl)
    assert m, "ORDER BY clause not found"
    first_key = m.group(1).split(",")[0].strip()
    assert first_key == "TenantId", f"TenantId must be first in ORDER BY, got {first_key!r}"


def test_cost_is_decimal_not_float(ddl):
    assert "EstimatedCost          Decimal64(8)" in ddl
    assert "ReconciledCost         Nullable(Decimal64(8))" in ddl
    assert "EstimatedCost          Float" not in ddl


def test_dual_track_and_key_version_columns(ddl):
    for col in ("CostSource", "PriceCatalogVersion", "UserIdHashKeyVersion"):
        assert col in ddl, f"missing required column {col}"


def test_required_codecs(ddl):
    assert "CODEC(Delta, ZSTD(1))" in ddl  # timestamp
    assert "CODEC(T64, ZSTD(1))" in ddl    # token counts


def test_bloom_filter_indexes(ddl):
    for idx in ("idx_trace_id", "idx_session_id", "idx_user_id", "idx_agent_run", "idx_attr_keys"):
        assert idx in ddl, f"missing index {idx}"
    assert "bloom_filter(0.001)" in ddl  # tight for trace/agent lookups


def test_daily_partition(ddl):
    assert "PARTITION BY toDate(Timestamp)" in ddl


def test_ttl_tiering(ddl):
    assert "TO VOLUME 'warm'" in ddl   # hot -> warm at 7d
    assert "INTERVAL 90 DAY DELETE" in ddl  # drop raw at 90d (aggregates live in MVs, CTO-24)


def test_no_invalid_ttl_group_by(ddl_code):
    # Guard against re-introducing a TTL GROUP BY whose keys are not a PK prefix (would fail on a
    # real cluster). Long-horizon aggregates belong in the rollup MVs (CTO-24), not here.
    # Checked against comment-stripped SQL so explanatory prose doesn't trip the guard.
    assert "GROUP BY" not in ddl_code.split("TTL", 1)[-1]


def test_balanced_parens(ddl):
    # cheap sanity check that the statement isn't truncated
    assert ddl.count("(") == ddl.count(")")
