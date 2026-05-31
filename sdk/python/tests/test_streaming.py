"""SSE tee + token reconstruction: completeness, tool calls, partial drops, latency (CTO-40)."""

from __future__ import annotations

import json

import pytest

from tally.schema import GenAI
from tally.streaming import (
    StreamReconstructor,
    StreamResult,
    StreamStatus,
    reconstruct,
    tee,
)


def _chunk(**body: object) -> str:
    """One OpenAI SSE frame."""
    return f"data: {json.dumps(body)}\n\n"


def _content_frame(text: str, *, model: str = "gpt-4o-mini") -> str:
    return _chunk(
        object="chat.completion.chunk",
        model=model,
        choices=[{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    )


def _stop_frame(*, model: str = "gpt-4o-mini") -> str:
    return _chunk(
        object="chat.completion.chunk",
        model=model,
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
    )


def _usage_frame(prompt: int, completion: int, cached: int = 0) -> str:
    return _chunk(
        object="chat.completion.chunk",
        model="gpt-4o-mini",
        choices=[],
        usage={
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    )


def _full_stream(text: str = "Hello world", *, prompt: int = 10, completion: int = 5) -> list[str]:
    """A clean streamed completion with include_usage, ending in [DONE].

    Content deltas preserve spacing (leading space on every word after the first) so the
    concatenated deltas reconstruct ``text`` exactly, like real OpenAI streaming.
    """
    words = text.split(" ")
    frames = [_content_frame(w if i == 0 else " " + w) for i, w in enumerate(words)]
    return [
        _chunk(object="chat.completion.chunk", model="gpt-4o-mini",
               choices=[{"index": 0, "delta": {"role": "assistant", "content": ""},
                         "finish_reason": None}]),
        *frames,
        _stop_frame(),
        _usage_frame(prompt, completion),
        "data: [DONE]\n\n",
    ]


# --- completeness / provider usage is authoritative ----------------------------------------------


def test_clean_stream_reconstructs_content_and_exact_usage() -> None:
    result = reconstruct(_full_stream("Hello world", prompt=10, completion=5))
    assert result.status is StreamStatus.COMPLETE
    assert result.content == "Hello world"
    assert result.model == "gpt-4o-mini"
    assert result.finish_reason == "stop"
    # Provider usage is authoritative — matches the reported numbers exactly.
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.usage_from_provider is True


def test_cached_prompt_tokens_are_captured() -> None:
    frames = _full_stream()
    frames[-2] = _usage_frame(10, 5, cached=4)
    result = reconstruct(frames)
    assert result.cached_input_tokens == 4


def test_done_without_usage_is_still_complete() -> None:
    frames = [_content_frame("hi"), _stop_frame(), "data: [DONE]\n\n"]
    result = reconstruct(frames)
    assert result.status is StreamStatus.COMPLETE
    # no provider usage → estimated output tokens, flagged as such
    assert result.usage_from_provider is False
    assert result.output_tokens is not None and result.output_tokens >= 1


# --- tool calls ----------------------------------------------------------------------------------


def test_tool_call_reconstructed_across_deltas() -> None:
    frames = [
        _chunk(model="gpt-4o-mini", choices=[{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "id": "call_1",
                            "function": {"name": "get_weather", "arguments": ""}}]}}]),
        _chunk(model="gpt-4o-mini", choices=[{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}]),
        _chunk(model="gpt-4o-mini", choices=[{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"arguments": '"NYC"}'}}]}}]),
        _stop_frame(),
        "data: [DONE]\n\n",
    ]
    result = reconstruct(frames)
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.call_id == "call_1"
    assert call.name == "get_weather"
    assert call.arguments == '{"city":"NYC"}'


# --- partial / mid-stream drop -------------------------------------------------------------------


def test_mid_stream_drop_is_partial_with_estimated_tokens() -> None:
    # 200 then the connection drops: content frames arrive, but no stop / usage / [DONE].
    frames = [_content_frame("partial "), _content_frame("answer that was cut")]
    result = reconstruct(frames, dropped=True)
    assert result.status is StreamStatus.PARTIAL
    assert result.content == "partial answer that was cut"
    # cost is computed on tokens actually received (estimated), not zero and not the full request.
    assert result.usage_from_provider is False
    assert result.output_tokens is not None and result.output_tokens >= 1


def test_incomplete_without_terminator_is_partial() -> None:
    # No explicit drop flag, but no clean terminator either → still partial.
    result = reconstruct([_content_frame("just one delta")])
    assert result.status is StreamStatus.PARTIAL


# --- tee: zero added latency (yield-before-feed) + drop handling ----------------------------------


def test_tee_forwards_all_chunks_in_order_unchanged() -> None:
    frames = _full_stream("a b c")
    rec = StreamReconstructor()
    forwarded = list(tee(frames, rec))
    assert forwarded == frames  # client path sees the bytes verbatim, in order
    assert rec.result().content == "a b c"


def test_tee_yields_before_feeding_telemetry() -> None:
    # The client must receive chunk N before the reconstructor has processed it (no buffering).
    rec = StreamReconstructor()
    seen_content_when_yielded: list[str] = []
    gen = tee([_content_frame("x"), _content_frame("y")], rec)
    next(gen)  # first chunk yielded to client...
    seen_content_when_yielded.append(rec.result().content)  # ...telemetry not yet fed it
    next(gen)
    seen_content_when_yielded.append(rec.result().content)
    # After the 1st yield, reconstructor had not yet consumed chunk 1 ("" content).
    assert seen_content_when_yielded[0] == ""
    assert seen_content_when_yielded[1] == "x"


def test_tee_marks_dropped_on_upstream_error_and_reraises() -> None:
    def broken() -> object:
        yield _content_frame("got this far")
        raise ConnectionError("upstream dropped")

    rec = StreamReconstructor()
    gen = tee(broken(), rec)
    assert next(gen) == _content_frame("got this far")
    with pytest.raises(ConnectionError):
        next(gen)
    assert rec.result().status is StreamStatus.PARTIAL


# --- robustness: never crash on malformed frames -------------------------------------------------


def test_malformed_frames_are_ignored_not_raised() -> None:
    frames = [
        "data: {not valid json\n\n",
        ": this is an sse comment\n\n",
        "\n",
        _content_frame("ok"),
        _stop_frame(),
        "data: [DONE]\n\n",
    ]
    result = reconstruct(frames)
    assert result.content == "ok"
    assert result.status is StreamStatus.COMPLETE


def test_frame_split_across_feed_boundaries_reconstructs() -> None:
    rec = StreamReconstructor()
    whole = _content_frame("split")
    half = len(whole) // 2
    rec.feed(whole[:half])
    rec.feed(whole[half:])
    rec.feed(_stop_frame())
    rec.feed("data: [DONE]\n\n")
    assert rec.result().content == "split"


def test_bytes_chunks_are_decoded() -> None:
    frames = [f.encode("utf-8") for f in _full_stream("byte stream")]
    result = reconstruct(frames)
    assert result.content == "byte stream"


# --- attribute projection ------------------------------------------------------------------------


def test_to_attributes_maps_gen_ai_keys() -> None:
    result: StreamResult = reconstruct(_full_stream("hi there", prompt=7, completion=3))
    attrs = result.to_attributes()
    assert attrs[GenAI.SYSTEM] == "openai"
    assert attrs[GenAI.RESPONSE_MODEL] == "gpt-4o-mini"
    assert attrs[GenAI.USAGE_INPUT_TOKENS] == 7
    assert attrs[GenAI.USAGE_OUTPUT_TOKENS] == 3
