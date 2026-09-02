# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §4 - patch_anthropic over fake Anthropic classes (create + stream, sync/async)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tally.instrumentation.patch import patch_anthropic, unpatch_all
from tally.pricing import seed_catalog
from tally.schema import GenAI, validate_span_attributes

_MODEL = "claude-sonnet-4-5"


@dataclass
class _U:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Resp:
    model: str
    usage: _U


def _start_event(model, input_tokens):
    msg = {"model": model, "usage": {"input_tokens": input_tokens}}
    return {"type": "message_start", "message": msg}


def _delta_event(output_tokens):
    return {"type": "message_delta", "usage": {"output_tokens": output_tokens}}


def _final_message(model, input_tokens, output_tokens):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return {"model": model, "usage": usage}


class _Stream:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)


class _StreamManager:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __enter__(self):
        return _Stream(self._events)

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeMessages:
    def create(self, *, model, input_tokens=100, output_tokens=50, cached=0, stream=False):
        if stream:
            # The supported streamed form of create(): returns an iterator of events.
            return iter([_start_event(model, input_tokens), _delta_event(output_tokens)])
        return _Resp(model, _U(input_tokens, output_tokens, cached))

    def stream(self, *, model, input_tokens=100, output_tokens=50, iterate=True):
        events = [_start_event(model, input_tokens), _delta_event(output_tokens)]
        final = _final_message(model, input_tokens, output_tokens)
        return _StreamManager(events if iterate else [], final)


class _AsyncStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e


class _AsyncStreamManager:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return _AsyncStream(self._events)

    async def __aexit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeAsyncMessages:
    async def create(self, *, model, input_tokens=100, output_tokens=50, stream=False):
        if stream:
            return _AsyncStream([_start_event(model, input_tokens), _delta_event(output_tokens)])
        return _Resp(model, _U(input_tokens, output_tokens))

    def stream(self, *, model, input_tokens=100, output_tokens=50):
        events = [_start_event(model, input_tokens), _delta_event(output_tokens)]
        final = _final_message(model, input_tokens, output_tokens)
        return _AsyncStreamManager(events, final)


class _AsyncFinalStream:
    """Entered async stream whose get_final_message is a coroutine, as the real client's is.

    Crucially the coroutine lives here, on the entered stream, not on the manager (finding #5).
    """

    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final


class _AsyncFinalStreamManager:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return _AsyncFinalStream(self._events, self._final)

    async def __aexit__(self, *exc):
        return False

    # Deliberately NO get_final_message here: it belongs on the entered stream object.


class FakeAsyncFinalMessages:
    def stream(self, *, model, input_tokens=100, output_tokens=50):
        # Empty events so the caller never iterates and the final-message path is exercised.
        final = _final_message(model, input_tokens, output_tokens)
        return _AsyncFinalStreamManager([], final)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    unpatch_all()


def _patch(spans):
    patch_anthropic(
        on_span=spans.append,
        catalog=seed_catalog(),
        targets={"messages": FakeMessages, "messages_async": FakeAsyncMessages},
    )


def test_sync_create_emits_span_with_usage():
    spans: list[dict] = []
    _patch(spans)
    resp = FakeMessages().create(
        model=_MODEL, input_tokens=1_000_000, output_tokens=1_000_000, cached=0
    )
    assert isinstance(resp, _Resp)
    assert validate_span_attributes(spans[0]) == []
    assert spans[0][GenAI.SYSTEM] == "anthropic"
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 1_000_000
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 1_000_000
    # 3.00 input + 15.00 output USD per 1M = 18_000_000 micro.
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] == 18_000_000


def test_cache_read_maps_to_cached_input():
    spans: list[dict] = []
    _patch(spans)
    FakeMessages().create(model=_MODEL, input_tokens=1000, output_tokens=10, cached=1000)
    assert spans[0][GenAI.USAGE_CACHED_INPUT_TOKENS] == 1000


def test_sync_stream_accumulates_usage():
    spans: list[dict] = []
    _patch(spans)
    collected = []
    with FakeMessages().stream(model=_MODEL, input_tokens=200, output_tokens=80) as stream:
        for event in stream:
            collected.append(event)
    assert len(collected) == 2  # events passed through untouched
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 200
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 80


def test_stream_without_iteration_uses_final_message():
    spans: list[dict] = []
    _patch(spans)
    with FakeMessages().stream(model=_MODEL, input_tokens=42, output_tokens=7, iterate=False):
        pass  # caller never iterates the events
    assert len(spans) == 1
    # Seeded from get_final_message() rather than fabricated.
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 42
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 7


def test_create_stream_true_accumulates_usage():
    # messages.create(stream=True) is a supported streamed form; before the fix it was wrapped
    # non-streaming and emitted null tokens with no cost (CTO-260 §4.3, finding #2).
    spans: list[dict] = []
    _patch(spans)
    stream = FakeMessages().create(model=_MODEL, stream=True, input_tokens=250, output_tokens=75)
    collected = list(stream)  # events pass through untouched
    assert len(collected) == 2
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 250
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 75
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] > 0


def test_async_create_stream_true_accumulates_usage():
    spans: list[dict] = []
    _patch(spans)

    async def _run():
        agen = await FakeAsyncMessages().create(
            model=_MODEL, stream=True, input_tokens=120, output_tokens=30
        )
        return [e async for e in agen]

    collected = asyncio.run(_run())
    assert len(collected) == 2
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 120
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 30


def test_async_stream_awaits_final_message_coroutine():
    # On the async client get_final_message is a coroutine on the entered stream. The emit path must
    # look it up there (not on the manager) and await it, else usage is null (finding #5).
    spans: list[dict] = []
    patch_anthropic(
        on_span=spans.append,
        catalog=seed_catalog(),
        targets={"messages_async": FakeAsyncFinalMessages},
    )

    async def _run():
        async with FakeAsyncFinalMessages().stream(
            model=_MODEL, input_tokens=42, output_tokens=7
        ):
            pass  # never iterate, forcing the final-message fallback

    asyncio.run(_run())
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 42
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 7


def test_stream_provider_error_propagates():
    class Boom:
        def stream(self, *, model):
            raise RuntimeError("anthropic 529")

    spans: list[dict] = []
    patch_anthropic(on_span=spans.append, targets={"messages": Boom})
    with pytest.raises(RuntimeError, match="anthropic 529"):
        Boom().stream(model=_MODEL)


def test_async_create_and_stream():
    spans: list[dict] = []
    _patch(spans)

    async def _run():
        resp = await FakeAsyncMessages().create(model=_MODEL, input_tokens=100, output_tokens=50)
        assert isinstance(resp, _Resp)
        collected = []
        mgr = FakeAsyncMessages().stream(model=_MODEL, input_tokens=300, output_tokens=90)
        async with mgr as s:
            async for e in s:
                collected.append(e)
        return collected

    collected = asyncio.run(_run())
    assert len(collected) == 2
    assert len(spans) == 2
    stream_span = spans[1]
    assert stream_span[GenAI.USAGE_INPUT_TOKENS] == 300
    assert stream_span[GenAI.USAGE_OUTPUT_TOKENS] == 90
