# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §4 - patch_openai over fake OpenAI classes (sync/async/streaming, no network)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tally.instrumentation.patch import patch_openai, unpatch_all
from tally.pricing import seed_catalog
from tally.schema import GenAI, validate_span_attributes


# --- fake OpenAI response + chunk shapes ---
@dataclass
class _U:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _Resp:
    model: str
    usage: _U


@dataclass
class _Chunk:
    model: str
    usage: _U | None = None


@dataclass
class _EmbUsage:
    prompt_tokens: int


@dataclass
class _EmbResp:
    model: str
    usage: _EmbUsage


class FakeCompletions:
    def __init__(self) -> None:
        self.received: list = []

    def create(self, *, model, stream=False, stream_options=None, prompt=10, completion=5):
        self.received.append(stream_options)
        if stream:
            chunks = [_Chunk(model=model)]
            if stream_options and stream_options.get("include_usage"):
                chunks.append(_Chunk(model=model, usage=_U(prompt, completion)))
            return iter(chunks)
        return _Resp(model, _U(prompt, completion))


class _AsyncChunks:
    def __init__(self, model, with_usage, prompt, completion):
        self._model = model
        self._with_usage = with_usage
        self._prompt = prompt
        self._completion = completion

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield _Chunk(model=self._model)
        if self._with_usage:
            yield _Chunk(model=self._model, usage=_U(self._prompt, self._completion))


class FakeAsyncCompletions:
    def __init__(self) -> None:
        self.received: list = []

    async def create(self, *, model, stream=False, stream_options=None, prompt=10, completion=5):
        self.received.append(stream_options)
        if stream:
            with_usage = bool(stream_options and stream_options.get("include_usage"))
            return _AsyncChunks(model, with_usage, prompt, completion)
        return _Resp(model, _U(prompt, completion))


class FakeEmbeddings:
    def create(self, *, model, tokens=1000):
        return _EmbResp(model, _EmbUsage(tokens))


# --- Responses API fakes (usage named input_tokens / output_tokens; terminal event nests it) ---
@dataclass
class _RespUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _RespObj:
    model: str
    usage: _RespUsage


@dataclass
class _RespEvent:
    response: _RespObj | None = None
    model: str | None = None


class FakeResponses:
    def create(self, *, model, stream=False, input_tokens=100, output_tokens=50):
        if stream:
            terminal = _RespEvent(response=_RespObj(model, _RespUsage(input_tokens, output_tokens)))
            return _OpenAIStream([_RespEvent(model=model), terminal])
        return _RespObj(model, _RespUsage(input_tokens, output_tokens))


class _OpenAIStream:
    """Mimics an ``openai`` ``Stream``: iterable, a context manager, with ``.response``/``.close``.

    This is the object ``create(stream=True)`` returns; the instrumentation must preserve it rather
    than replace it with a bare generator (CTO-260 §4.3, review finding #6).
    """

    def __init__(self, chunks, response="HTTP-RESP"):
        self._chunks = chunks
        self.response = response
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    unpatch_all()


def _patch(spans, **kw):
    patch_openai(
        on_span=spans.append,
        catalog=seed_catalog(),
        targets={
            "chat": FakeCompletions,
            "chat_async": FakeAsyncCompletions,
            "embeddings": FakeEmbeddings,
        },
        **kw,
    )


def test_sync_chat_emits_conformant_span():
    spans: list[dict] = []
    _patch(spans)
    client = FakeCompletions()
    resp = client.create(model="gpt-4o-mini", prompt=1_000_000, completion=1_000_000)
    assert isinstance(resp, _Resp)  # original returned unchanged
    assert len(spans) == 1
    assert validate_span_attributes(spans[0]) == []
    assert spans[0][GenAI.SYSTEM] == "openai"
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 1_000_000
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] > 0


def test_idempotent_double_patch_emits_once():
    spans: list[dict] = []
    _patch(spans)
    _patch(spans)  # second patch is a no-op
    FakeCompletions().create(model="gpt-4o-mini")
    assert len(spans) == 1


def test_uninstrument_reverses():
    spans: list[dict] = []
    _patch(spans)
    unpatch_all()
    FakeCompletions().create(model="gpt-4o-mini")
    assert spans == []


def test_provider_error_propagates():
    class Boom:
        def create(self, *, model):
            raise RuntimeError("openai 500")

    spans: list[dict] = []
    patch_openai(on_span=spans.append, targets={"chat": Boom})
    with pytest.raises(RuntimeError, match="openai 500"):
        Boom().create(model="gpt-4o-mini")


def test_faulty_sink_never_breaks_call():
    def bad_sink(_attrs):
        raise ValueError("sink boom")

    patch_openai(
        on_span=bad_sink, catalog=seed_catalog(), targets={"chat": FakeCompletions}
    )
    resp = FakeCompletions().create(model="gpt-4o-mini")
    assert isinstance(resp, _Resp)


def test_streaming_without_usage_yields_null_tokens():
    spans: list[dict] = []
    _patch(spans)
    stream = FakeCompletions().create(model="gpt-4o-mini", stream=True)
    chunks = list(stream)  # caller iterates untouched chunks
    assert len(chunks) == 1  # no usage chunk since include_usage not set
    assert len(spans) == 1
    # Honest blank: tokens null, never a fabricated zero.
    assert GenAI.USAGE_INPUT_TOKENS not in spans[0]
    assert GenAI.COST_ESTIMATED_MICRO_USD not in spans[0]


def test_stream_usage_optin_injects_include_usage_and_prices():
    spans: list[dict] = []
    _patch(spans, instrument_stream_usage=True)
    client = FakeCompletions()
    chunks = list(client.create(model="gpt-4o-mini", stream=True, prompt=1000, completion=500))
    # The wrapper added stream_options when the caller did not set it.
    assert client.received[-1] == {"include_usage": True}
    assert len(chunks) == 2  # usage chunk now present
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 1000
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 500


def test_stream_usage_respects_caller_options():
    spans: list[dict] = []
    _patch(spans, instrument_stream_usage=True)
    client = FakeCompletions()
    list(client.create(model="gpt-4o-mini", stream=True, stream_options={"include_usage": True}))
    # Caller already set it; the wrapper must not overwrite.
    assert client.received[-1] == {"include_usage": True}


def test_embeddings_priced_under_embedding_tier():
    spans: list[dict] = []
    _patch(spans)
    FakeEmbeddings().create(model="text-embedding-3-small", tokens=1_000_000)
    assert len(spans) == 1
    assert spans[0][GenAI.OPERATION_NAME] == "embeddings"
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] == 20_000  # 0.02 USD / 1M tokens


def test_async_chat_and_stream():
    spans: list[dict] = []
    _patch(spans)

    async def _run():
        resp = await FakeAsyncCompletions().create(model="gpt-4o-mini", prompt=100, completion=50)
        assert isinstance(resp, _Resp)
        agen = await FakeAsyncCompletions().create(model="gpt-4o-mini", stream=True)
        out = [c async for c in agen]
        return out

    chunks = asyncio.run(_run())
    assert len(chunks) == 1  # no usage without include_usage
    # Two spans: one from the non-stream call, one from the exhausted stream.
    assert len(spans) == 2


def test_responses_nonstream_emits_span():
    spans: list[dict] = []
    patch_openai(
        on_span=spans.append, catalog=seed_catalog(), targets={"responses": FakeResponses}
    )
    FakeResponses().create(model="gpt-4o-mini", input_tokens=10, output_tokens=5)
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 10
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 5


def test_responses_stream_accumulates_usage():
    # Before the fix responses.create(stream=True) was wrapped non-streaming and emitted null
    # tokens with no cost. It must now accumulate usage from the stream (CTO-260 §4.2, finding #3).
    spans: list[dict] = []
    patch_openai(
        on_span=spans.append, catalog=seed_catalog(), targets={"responses": FakeResponses}
    )
    stream = FakeResponses().create(
        model="gpt-4o-mini", stream=True, input_tokens=1000, output_tokens=500
    )
    chunks = list(stream)
    assert len(chunks) == 2  # events passed through untouched
    assert len(spans) == 1
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 1000
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 500
    assert spans[0][GenAI.COST_ESTIMATED_MICRO_USD] > 0


def test_openai_stream_preserves_context_manager_and_attributes():
    # The OpenAI streaming wrapper must preserve the provider Stream object: attribute delegation
    # (.response), close(), and the with-statement protocol (CTO-260 §4.3, finding #6).
    spans: list[dict] = []

    class FakeStreamChat:
        def create(self, *, model, stream=False, stream_options=None):
            chunks = [_Chunk(model=model), _Chunk(model=model, usage=_U(100, 50))]
            return _OpenAIStream(chunks)

    patch_openai(
        on_span=spans.append, catalog=seed_catalog(), targets={"chat": FakeStreamChat}
    )
    client = FakeStreamChat()
    collected = []
    with client.create(model="gpt-4o-mini", stream=True) as s:
        assert s.response == "HTTP-RESP"  # attribute delegated to the real Stream
        for chunk in s:
            collected.append(chunk)
    assert len(collected) == 2
    assert s.closed is True  # __exit__ delegated to the real Stream.close()
    assert len(spans) == 1  # span emitted exactly once
    assert spans[0][GenAI.USAGE_INPUT_TOKENS] == 100
    assert spans[0][GenAI.USAGE_OUTPUT_TOKENS] == 50
