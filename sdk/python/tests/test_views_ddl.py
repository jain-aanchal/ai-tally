# SPDX-License-Identifier: Apache-2.0
"""Structural validation for the rollup MV, last_touch_index, and attribution DDL.

CTO-24/25/26. ClickHouse isn't available in CI, so we assert the spec-load-bearing invariants:
TenantId-first ordering, MVs read from otel_spans, UserIdHashKeyVersion present where bridging
needs it, idempotency keys, and balanced parens (not truncated).
"""

from __future__ import annotations

from pathlib import Path

import pytest

DB = Path(__file__).resolve().parents[3] / "db" / "clickhouse"


def _read(name: str) -> str:
    p = DB / name
    assert p.exists(), f"missing DDL {p}"
    return p.read_text()


@pytest.fixture(scope="module")
def rollups() -> str:
    return _read("rollups.sql")


@pytest.fixture(scope="module")
def last_touch() -> str:
    return _read("last_touch_index.sql")


@pytest.fixture(scope="module")
def attribution() -> str:
    return _read("attribution.sql")


def test_rollups_read_from_spans_and_sum(rollups):
    assert rollups.count("FROM otel_spans") == 2  # daily + hourly
    assert "SummingMergeTree" in rollups
    assert "daily_feature_rollup" in rollups and "hourly_feature_rollup" in rollups


def test_rollups_tenant_first(rollups):
    for clause in rollups.split("ORDER BY ("):
        first = clause.split(")")[0].split(",")[0].strip()
        if first and not first.startswith("--"):
            # every ORDER BY in this file starts with TenantId
            assert first == "TenantId", f"expected TenantId-first, got {first!r}"


def test_last_touch_replacing_and_keyed(last_touch):
    assert "ReplacingMergeTree(UpdatedAt)" in last_touch
    assert "ORDER BY (TenantId, UserIdHash, FeatureTag)" in last_touch
    assert "FROM otel_spans" in last_touch
    assert "UserIdHashKeyVersion" in last_touch  # cross-version bridging


def test_attribution_has_four_tables(attribution):
    for t in ("identity_graph", "business_events", "attribution_records", "unattributed_events"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in attribution


def test_attribution_idempotent_key(attribution):
    # attribution_records idempotent on (TenantId, BusinessEventId, FeatureTag)
    assert "ORDER BY (TenantId, BusinessEventId, FeatureTag)" in attribution


def test_attribution_carries_key_version(attribution):
    # identity_graph + attribution_records need the HMAC key version for bridging
    assert attribution.count("UserIdHashKeyVersion") >= 2


def test_unattributed_is_modeled(attribution):
    assert "unattributed_events" in attribution
    assert "no_trace_in_window" in attribution  # reasons enumerated, not silent drop


@pytest.mark.parametrize("name", ["rollups.sql", "last_touch_index.sql", "attribution.sql"])
def test_balanced_parens(name):
    sql = _read(name)
    assert sql.count("(") == sql.count(")")
