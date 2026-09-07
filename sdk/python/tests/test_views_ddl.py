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


def test_identity_graph_carries_account_id(attribution):
    # CTO-184: a CRM/CDP connector stitches users to accounts through this enum.
    assert attribution.count("'account_id'=6") >= 2  # IdentityAType and IdentityBType


def test_identity_type_enum_ordinals_are_never_renumbered(attribution):
    # An Enum8 is stored on disk as its ordinal, so reusing or renumbering a value silently
    # reinterprets every row already written. These five are frozen forever; new values append.
    frozen = {
        "'user_id'": 1,
        "'anonymous_id'": 2,
        "'session_id'": 3,
        "'email'": 4,
        "'external_id'": 5,
    }
    for name, ordinal in frozen.items():
        assert f"{name}={ordinal}" in attribution
        # And no other ordinal is ever attached to that name.
        for other in range(1, 10):
            if other != ordinal:
                assert f"{name}={other}" not in attribution


def test_identity_graph_enum_widening_has_a_migration_path(attribution):
    # initdb only fires on a fresh volume, so an existing deployment needs an explicit ALTER
    # (replayed idempotently by `make ch-migrate`). Enum widening is MODIFY, not ADD COLUMN.
    assert "MODIFY COLUMN IdentityAType" in attribution
    assert "MODIFY COLUMN IdentityBType" in attribution


def test_unattributed_is_modeled(attribution):
    assert "unattributed_events" in attribution
    assert "no_trace_in_window" in attribution  # reasons enumerated, not silent drop


def _unknown_usage_predicates(rollups: str) -> list[str]:
    """The body of every `countIf(...) AS UnknownUsageSpanCount` in the file, one per MV."""
    out: list[str] = []
    for chunk in rollups.split("AS UnknownUsageSpanCount")[:-1]:
        start = chunk.rindex("countIf(")
        out.append(" ".join(chunk[start:].split()))
    return out


def test_unknown_usage_never_counts_a_priced_span(rollups):
    """CTO-244 follow-up: the invariant the counter's name promises, asserted structurally.

    A span counted as unknown-usage must NEVER also carry a priced cost. The predicate enforces
    that by construction with an `EstimatedCost IS NULL` conjunct, so this test fails loudly if a
    later edit drops it. Both MVs write the same SummingMergeTree column, so both are checked.
    """
    predicates = _unknown_usage_predicates(rollups)
    assert len(predicates) == 2, "expected the daily and hourly MV predicates"
    for p in predicates:
        assert p.startswith("countIf( otel_spans.EstimatedCost IS NULL AND"), p


def test_unknown_usage_is_per_operation_kind(rollups):
    """An embedding has no output side, and tool / vector spans have no token usage at all.

    The old chat-shaped `InputTokens IS NULL OR OutputTokens IS NULL` flagged all three as
    unknown-usage even when they were correctly priced per call. The predicate branches on
    GenAiOperation, the same discriminator the cost layers and enrich_cost use.
    """
    for p in _unknown_usage_predicates(rollups):
        assert "otel_spans.GenAiOperation = 'embeddings', otel_spans.InputTokens IS NULL" in p
        assert "otel_spans.GenAiOperation IN ('tool', 'vector', 'compute', 'egress'), 0" in p
        # The chat fallback keeps both sides required: a chat call is priced from both.
        assert p.rstrip(") ").endswith(
            "otel_spans.InputTokens IS NULL OR otel_spans.OutputTokens IS NULL"
        ), p


def test_both_rollup_mvs_count_unknown_usage_identically(rollups):
    daily, hourly = _unknown_usage_predicates(rollups)
    assert daily == hourly


@pytest.mark.parametrize("name", ["rollups.sql", "last_touch_index.sql", "attribution.sql"])
def test_balanced_parens(name):
    sql = _read(name)
    assert sql.count("(") == sql.count(")")
