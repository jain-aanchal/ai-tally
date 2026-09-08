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
    # CTO-244: absent usage and an unpriceable call are NULL, not 0. See the tests below.
    assert row["EstimatedCost"] is None
    assert row["InputTokens"] is None
    # trace/span ids are generated when absent
    assert row["TraceId"] and isinstance(row["TraceId"], str)
    assert row["SpanId"] and isinstance(row["SpanId"], str)


# --- CTO-244: unknown must be representable, and distinguishable from a real zero --------------


def test_absent_usage_maps_to_null_not_zero() -> None:
    """The streamed-response case: the provider never reported usage, so we do not know it.

    Writing 0 here told the dashboard a real, billed call consumed nothing.
    """
    row = _row_dict(span_to_row({GenAI.SYSTEM: "openai"}, tenant_id="t1", effective_ts_ns=0))
    assert row["InputTokens"] is None
    assert row["OutputTokens"] is None
    assert row["CachedInputTokens"] is None


def test_provider_reported_zero_stays_zero_and_is_distinct_from_unknown() -> None:
    attrs = {
        GenAI.USAGE_INPUT_TOKENS: 0,
        GenAI.USAGE_OUTPUT_TOKENS: 0,
        GenAI.USAGE_CACHED_INPUT_TOKENS: 0,
    }
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    assert row["InputTokens"] == 0
    assert row["OutputTokens"] == 0
    assert row["CachedInputTokens"] == 0
    # The distinction that did not exist before: a real 0 is not None.
    assert row["InputTokens"] is not None


def test_unparseable_usage_maps_to_null_not_zero() -> None:
    attrs = {GenAI.USAGE_INPUT_TOKENS: "lots", GenAI.USAGE_OUTPUT_TOKENS: True}
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    # A bool is not a token count; neither is a string. Guessing 0 would be a fabricated number.
    assert row["InputTokens"] is None
    assert row["OutputTokens"] is None


def test_catalog_miss_yields_null_cost_with_a_recorded_reason() -> None:
    """No priced cost on the span (a catalog miss) must not become $0.00.

    The reason reuses the existing cost-source notion: CostSource = 'unpriced', with the empty
    PriceCatalogVersion that tally.pricing already returns when a rate is missing.
    """
    attrs = {
        GenAI.SYSTEM: "openai",
        GenAI.REQUEST_MODEL: "some-model-the-catalog-has-never-heard-of",
        GenAI.USAGE_INPUT_TOKENS: 1000,
        GenAI.USAGE_OUTPUT_TOKENS: 250,
    }
    row = _row_dict(span_to_row(attrs, tenant_id="t1", effective_ts_ns=0))
    assert row["EstimatedCost"] is None
    assert row["CostSource"] == "unpriced"
    assert row["PriceCatalogVersion"] == ""
    # Usage was reported even though the cost could not be derived: the two are independent.
    assert row["InputTokens"] == 1000


def test_priced_zero_cost_is_estimated_not_unpriced() -> None:
    """A genuine 0 micro-USD (e.g. a free-tier rate) is a real price, not an unknown."""
    row = _row_dict(
        span_to_row({GenAI.COST_ESTIMATED_MICRO_USD: 0}, tenant_id="t1", effective_ts_ns=0)
    )
    assert row["EstimatedCost"] == Decimal(0)
    assert row["CostSource"] == "estimated"


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
