"""Pure tests for protocol negotiation, capabilities, and OTLP/HTTP translation (CTO-31)."""

from __future__ import annotations

from tally.schema import GenAI

from gateway.protocol import (
    INGEST_V1,
    SUPPORTED_PROTOCOLS,
    capabilities,
    negotiate,
    otlp_traces_to_spans,
)


def test_negotiate_default_when_unspecified() -> None:
    assert negotiate(None) == INGEST_V1
    assert negotiate("") == INGEST_V1


def test_negotiate_known_protocol() -> None:
    assert negotiate(INGEST_V1) == INGEST_V1


def test_negotiate_unknown_is_none() -> None:
    assert negotiate("ingest-v999") is None
    assert negotiate("totally-made-up") is None


def test_capabilities_shape() -> None:
    caps = capabilities(max_batch_size=2000, max_span_bytes=65536)
    assert caps["default_protocol"] == INGEST_V1
    assert list(SUPPORTED_PROTOCOLS) == caps["protocols"]
    assert caps["max_batch_size"] == 2000
    assert caps["max_span_bytes"] == 65536
    assert caps["features"]["idempotency"] is True
    assert caps["features"]["otlp_http_traces"] is True


# --- OTLP/HTTP translation ------------------------------------------------------------------------


def _otlp_doc() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout-api"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "abc123",
                                "spanId": "def456",
                                "startTimeUnixNano": "1717000000000000000",
                                "attributes": [
                                    {"key": GenAI.SYSTEM, "value": {"stringValue": "openai"}},
                                    {"key": GenAI.OPERATION_NAME, "value": {"stringValue": "chat"}},
                                    {
                                        "key": GenAI.USAGE_INPUT_TOKENS,
                                        "value": {"intValue": "120"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_otlp_translation_maps_structural_keys() -> None:
    spans = otlp_traces_to_spans(_otlp_doc())
    assert len(spans) == 1
    s = spans[0]
    assert s["trace_id"] == "abc123"
    assert s["span_id"] == "def456"
    assert s["timestamp_ns"] == 1717000000000000000
    assert s["ServiceName"] == "checkout-api"
    assert s[GenAI.SYSTEM] == "openai"


def test_otlp_intvalue_coerced_to_int() -> None:
    s = otlp_traces_to_spans(_otlp_doc())[0]
    assert s[GenAI.USAGE_INPUT_TOKENS] == 120
    assert isinstance(s[GenAI.USAGE_INPUT_TOKENS], int)


def test_otlp_span_attrs_win_over_resource() -> None:
    doc = _otlp_doc()
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
        {"key": "service.name", "value": {"stringValue": "override"}}
    )
    s = otlp_traces_to_spans(doc)[0]
    assert s["service.name"] == "override"


def test_otlp_empty_doc_is_empty_list() -> None:
    assert otlp_traces_to_spans({}) == []
    assert otlp_traces_to_spans({"resourceSpans": []}) == []


def test_otlp_value_variants() -> None:
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "s",
                                "attributes": [
                                    {"key": "d", "value": {"doubleValue": 1.5}},
                                    {"key": "b", "value": {"boolValue": True}},
                                    {
                                        "key": "arr",
                                        "value": {
                                            "arrayValue": {
                                                "values": [{"stringValue": "x"}, {"intValue": "2"}]
                                            }
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    s = otlp_traces_to_spans(doc)[0]
    assert s["d"] == 1.5
    assert s["b"] is True
    assert s["arr"] == ["x", 2]
