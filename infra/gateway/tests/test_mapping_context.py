"""Span -> row promotion of the CTO-118 context-drop attributes.

Two contracts checked here:

1. Presence — the three ``gen_ai.context.*`` attributes land in their typed columns,
   absent ones default to 0.
2. The hard "no bodies in telemetry" guard — even if a caller sends a key like
   ``message_text`` or ``gen_ai.prompt.text`` carrying a real prompt, the gateway
   does NOT persist it (neither as a column nor in ``SpanAttributes``).
"""

from __future__ import annotations

from gateway.mapping import COLUMNS, _is_body_key, span_to_row


def _row_dict(row: tuple[object, ...]) -> dict[str, object]:
    assert len(row) == len(COLUMNS)
    return dict(zip(COLUMNS, row, strict=True))


def test_context_drop_attrs_promoted_to_columns() -> None:
    span = {
        "gen_ai.context.dropped_messages": 4,
        "gen_ai.context.dropped_tokens": 1800,
        "gen_ai.context.window_used_pct": 0.93,
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    assert row["ContextDroppedMessages"] == 4
    assert row["ContextDroppedTokens"] == 1800
    assert row["ContextWindowUsedPct"] == 0.93


def test_context_drop_columns_default_to_zero_when_absent() -> None:
    """A span without any drop attrs must produce zeros, not garbage."""
    row = _row_dict(span_to_row({}, tenant_id="t1", effective_ts_ns=0))
    assert row["ContextDroppedMessages"] == 0
    assert row["ContextDroppedTokens"] == 0
    assert row["ContextWindowUsedPct"] == 0.0


def test_context_drop_attrs_not_duplicated_in_span_attributes_map() -> None:
    """Promoted keys must not also appear in the SpanAttributes long-tail map."""
    span = {
        "gen_ai.context.dropped_messages": 1,
        "gen_ai.context.dropped_tokens": 50,
        "gen_ai.context.window_used_pct": 0.4,
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    assert "gen_ai.context.dropped_messages" not in extra
    assert "gen_ai.context.dropped_tokens" not in extra
    assert "gen_ai.context.window_used_pct" not in extra


def test_window_pct_out_of_range_clamped() -> None:
    """Float coercion clamps to [0, 1] — a buggy/malicious caller can't poison the column."""
    span = {"gen_ai.context.window_used_pct": 1.7}
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    assert row["ContextWindowUsedPct"] == 1.0

    span2 = {"gen_ai.context.window_used_pct": -0.5}
    row2 = _row_dict(span_to_row(span2, tenant_id="t1", effective_ts_ns=0))
    assert row2["ContextWindowUsedPct"] == 0.0


# --- No bodies in telemetry (the load-bearing PII guard) ----------------------------------------


def test_pii_guard_drops_message_text_key() -> None:
    """A caller that tries to ship the actual dropped prompt under a familiar name must
    have it stripped — not stored in any column, not stored in the long-tail map."""
    leaked = "The user's full medical history: ..."
    span = {
        "gen_ai.context.dropped_messages": 2,
        "gen_ai.context.dropped_tokens": 400,
        "message_text": leaked,
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    # Counts present.
    assert row["ContextDroppedMessages"] == 2
    assert row["ContextDroppedTokens"] == 400
    # Body absent — not in the map, not anywhere.
    assert "message_text" not in extra
    for v in extra.values():
        assert leaked not in v


def test_pii_guard_drops_namespaced_body_keys() -> None:
    """Nested namespaces don't bypass the guard — we match by trailing segment."""
    leaked = "secret prompt body"
    span = {
        "gen_ai.prompt.text": leaked,
        "tally.completion": leaked,
        "anything.content": leaked,
        "user.input_text": leaked,
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    for v in extra.values():
        assert leaked not in v


def test_is_body_key_unit() -> None:
    """Direct unit on the helper — defends against well-meaning refactors."""
    assert _is_body_key("message_text")
    assert _is_body_key("gen_ai.prompt.text")
    assert _is_body_key("X.Y.completion")
    assert _is_body_key("MESSAGES")
    # Negatives — counts and metadata must pass.
    assert not _is_body_key("gen_ai.context.dropped_messages")
    assert not _is_body_key("gen_ai.usage.input_tokens")
    assert not _is_body_key("gen_ai.feature_tag")
