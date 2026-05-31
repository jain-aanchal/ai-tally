"""Pure tests for per-item span validation + PII rejection (CTO-34). No infra."""

from __future__ import annotations

from tally.schema import GenAI

from gateway.errors import ErrorCode
from gateway.validation import SpanValidator, span_item_id


def _v(**kw) -> SpanValidator:
    return SpanValidator(**kw)


def _good_span() -> dict[str, object]:
    return {
        "trace_id": "abc",
        "span_id": "def",
        GenAI.SYSTEM: "openai",
        GenAI.OPERATION_NAME: "chat",
        GenAI.USAGE_INPUT_TOKENS: 100,
        GenAI.COST_ESTIMATED_MICRO_USD: 750,
        GenAI.COST_CURRENCY: "USD",
        GenAI.FEATURE_TAG: "assistant",
    }


def test_conformant_span_accepted() -> None:
    r = _v().validate(_good_span())
    assert r.accepted is True
    assert r.rejection is None
    assert r.flags == []


def test_non_dict_is_invalid_schema() -> None:
    r = _v().validate("not a span")
    assert r.accepted is False
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_wrong_int_type_is_invalid_schema() -> None:
    span = _good_span()
    span[GenAI.USAGE_INPUT_TOKENS] = "100"  # string, not int
    r = _v().validate(span)
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_negative_token_is_invalid_schema() -> None:
    span = _good_span()
    span[GenAI.USAGE_OUTPUT_TOKENS] = -1
    r = _v().validate(span)
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_bool_is_not_accepted_as_int() -> None:
    span = _good_span()
    span[GenAI.USAGE_INPUT_TOKENS] = True
    r = _v().validate(span)
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_uppercase_operation_is_invalid() -> None:
    span = _good_span()
    span[GenAI.OPERATION_NAME] = "Chat"
    r = _v().validate(span)
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_bad_currency_is_invalid() -> None:
    span = _good_span()
    span[GenAI.COST_CURRENCY] = "DOLLARS"
    r = _v().validate(span)
    assert r.rejection == ErrorCode.INVALID_SCHEMA


def test_unknown_keys_are_tolerated() -> None:
    span = _good_span()
    span["gen_ai.custom.flag"] = "x"
    span["some_future_field"] = 42
    r = _v().validate(span)
    assert r.accepted is True


# --- PII ------------------------------------------------------------------------------------------


def test_raw_email_in_any_value_is_pii() -> None:
    span = _good_span()
    span["gen_ai.prompt.snippet"] = "contact me at alice@example.com please"
    r = _v().validate(span)
    assert r.accepted is False
    assert r.rejection == ErrorCode.PII_DETECTED


def test_forbidden_pii_key_is_rejected() -> None:
    span = _good_span()
    span["email"] = "x"
    r = _v().validate(span)
    assert r.rejection == ErrorCode.PII_DETECTED


def test_unhashed_user_id_hash_is_pii() -> None:
    span = _good_span()
    span[GenAI.USER_ID_HASH] = "alice@example.com"
    r = _v().validate(span)
    assert r.rejection == ErrorCode.PII_DETECTED


def test_raw_username_as_hash_is_pii() -> None:
    span = _good_span()
    span[GenAI.USER_ID_HASH] = "alice_smith"  # not hex
    r = _v().validate(span)
    assert r.rejection == ErrorCode.PII_DETECTED


def test_real_hash_accepted() -> None:
    span = _good_span()
    span[GenAI.USER_ID_HASH] = "a" * 64  # 64 hex chars
    r = _v().validate(span)
    assert r.accepted is True


# --- size -----------------------------------------------------------------------------------------


def test_oversized_span_is_payload_too_large() -> None:
    span = _good_span()
    span["blob"] = "x" * 2048
    r = _v(max_span_bytes=1024).validate(span)
    assert r.rejection == ErrorCode.PAYLOAD_TOO_LARGE


# --- unknown feature tag (flag, not rejection) ----------------------------------------------------


def test_unknown_feature_tag_is_flagged_not_rejected() -> None:
    span = _good_span()
    span[GenAI.FEATURE_TAG] = "brand_new_feature"
    r = _v(known_feature_tags={"assistant", "summarize"}).validate(span)
    assert r.accepted is True
    assert ErrorCode.UNKNOWN_FEATURE_TAG in r.flags


def test_known_feature_tag_not_flagged() -> None:
    span = _good_span()
    r = _v(known_feature_tags={"assistant"}).validate(span)
    assert r.accepted is True
    assert r.flags == []


def test_no_known_tags_disables_flag() -> None:
    span = _good_span()
    span[GenAI.FEATURE_TAG] = "whatever"
    r = _v(known_feature_tags=None).validate(span)
    assert r.accepted is True
    assert r.flags == []


# --- item id --------------------------------------------------------------------------------------


def test_item_id_prefers_trace_span() -> None:
    assert span_item_id({"trace_id": "t", "span_id": "s"}, 3) == "t:s"
    assert span_item_id({}, 3) == "#3"
