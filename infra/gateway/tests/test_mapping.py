"""Pure tests for span -> otel_spans row mapping (no infra needed)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tally.schema import GenAI, SpanFields, build_span_attributes

from gateway.mapping import COLUMNS, span_to_row


def _row_dict(row: tuple[object, ...]) -> dict[str, object]:
    assert len(row) == len(COLUMNS)
    return dict(zip(COLUMNS, row, strict=True))


def test_maps_genai_attrs_to_typed_columns() -> None:
    attrs = build_span_attributes(
        SpanFields(
            system="openai",
            request_model="gpt-5-mini",
            response_model="gpt-5-mini",
            operation="chat",
            input_tokens=1000,
            output_tokens=250,
            cost_estimated_micro_usd=750,
            feature_tag="assistant",
            session_id="sess-1",
        )
    )
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=1_700_000_000_000_000_000))

    assert row["TenantId"] == "t1"
    assert row["GenAiSystem"] == "openai"
    assert row["GenAiResponseModel"] == "gpt-5-mini"
    assert row["GenAiOperation"] == "chat"
    assert row["InputTokens"] == 1000
    assert row["OutputTokens"] == 250
    assert row["FeatureTag"] == "assistant"
    assert row["SessionId"] == "sess-1"
    assert row["CostSource"] == "estimated"
    assert row["CostCurrency"] == "USD"


def test_cost_micro_usd_becomes_decimal_usd() -> None:
    attrs = {GenAI.COST_ESTIMATED_MICRO_USD: 2_500_000, GenAI.COST_CURRENCY: "USD"}
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    assert row["EstimatedCost"] == Decimal("2.50000000")


def test_timestamp_is_utc_datetime_from_ns() -> None:
    ns = 1_700_000_000_000_000_000
    row = _row_dict(span_to_row({}, tenant_id="t1", effective_ts_ns=ns))
    assert row["Timestamp"] == datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def test_defaults_for_missing_fields() -> None:
    row = _row_dict(span_to_row({}, tenant_id="t1", effective_ts_ns=0))
    assert row["FeatureTag"] == "untagged"
    assert row["ServiceName"] == "unknown"
    assert row["SpanName"] == "llm.call"
    assert row["EstimatedCost"] == Decimal(0)
    assert row["InputTokens"] == 0
    # trace/span ids are generated when absent
    assert row["TraceId"] and isinstance(row["TraceId"], str)
    assert row["SpanId"] and isinstance(row["SpanId"], str)


def test_unpromoted_attrs_land_in_span_attributes_map() -> None:
    attrs = {GenAI.SYSTEM: "openai", "gen_ai.custom.flag": "x", "gen_ai.tool.call_id": "tc-1"}
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    assert extra["gen_ai.custom.flag"] == "x"
    assert extra["gen_ai.tool.call_id"] == "tc-1"
    # promoted key must NOT be duplicated into the map
    assert GenAI.SYSTEM not in extra


def test_structural_keys_are_used_not_mapped() -> None:
    attrs = {"trace_id": "abc", "span_id": "def", "ServiceName": "api", GenAI.SYSTEM: "openai"}
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    assert row["TraceId"] == "abc"
    assert row["SpanId"] == "def"
    assert row["ServiceName"] == "api"
    assert "trace_id" not in row["SpanAttributes"]
