"""SSE stream tee + token reconstruction (CTO-40 / spec §4.2).

A streaming LLM response must reach the customer's client with **zero added latency**, while we
still capture the *complete* message and its token usage for cost. The proxy solves this by
**teeing** the upstream byte stream: the client path is forwarded unbuffered, and a copy is fed to
an async telemetry path that reconstructs the full message off the hot path.

This module is the transport-agnostic, fully-testable core of that telemetry path:

* :func:`tee` — a pass-through generator. It yields each upstream chunk to the client *immediately*
  (before doing any work), then feeds a copy into a :class:`StreamReconstructor`. Yielding first is
  what guarantees the tee adds no latency to the client path; reconstruction happens between yields
  and never blocks a chunk.
* :class:`StreamReconstructor` — incrementally parses OpenAI Chat Completions SSE
  (``data: {json}\\n\\n`` framing, terminated by ``data: [DONE]``), reassembling the streamed
  content and tool-call deltas and capturing the authoritative ``usage`` object emitted as the
  final chunk when the request set ``stream_options.include_usage``.
* **Partial streams:** a connection that returns ``200`` then drops mid-stream never reaches
  ``[DONE]`` / the usage chunk. That is reported as :class:`StreamStatus.PARTIAL`, and output
  tokens are *estimated* from the bytes actually received (flagged ``usage_from_provider=False``)
  so cost is computed on what the customer actually got, not zero and not the full request.

The real proxy's hot-path tee is implemented in the edge proxy (Go/Rust, CTO-39); this Python module
is the reference reconstruction logic the proxy mirrors, and the SDK's own streaming wrapper uses it
directly. Provider coverage here is OpenAI SSE; Anthropic SSE / Vertex chunked-JSON are follow-ups.
Cost calculation itself is CTO-35 — this module only produces the normalized usage.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum

from tally.schema import GenAI

_DONE = "[DONE]"
_DATA_PREFIX = "data:"


class StreamStatus(str, Enum):
    """Outcome of a reconstructed stream."""

    COMPLETE = "complete"  # terminated cleanly (finish_reason / usage / [DONE] seen)
    PARTIAL = "partial"  # 200 then dropped before a clean terminator


def _heuristic_output_tokens(text: str) -> int:
    """Rough output-token estimate when the provider never reported usage (~4 chars/token).

    Deliberately conservative and clearly *estimated* (callers see ``usage_from_provider=False``).
    A precise count needs the model's tokenizer; this is the floor used so a dropped stream still
    bills for what was received instead of zero.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class ReconstructedToolCall:
    """A tool call reassembled from streamed ``tool_calls`` deltas."""

    index: int
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(slots=True)
class StreamResult:
    """The reconstructed view of a streamed completion."""

    status: StreamStatus
    model: str | None = None
    content: str = ""
    finish_reason: str | None = None
    tool_calls: list[ReconstructedToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: True when token counts came from the provider ``usage`` chunk (exact); False when estimated.
    usage_from_provider: bool = False

    def to_attributes(self) -> dict[str, object]:
        """Project onto a ``gen_ai.*`` attribute dict (only the keys that are known)."""
        attrs: dict[str, object] = {GenAI.SYSTEM: "openai", GenAI.OPERATION_NAME: "chat"}
        if self.model is not None:
            attrs[GenAI.RESPONSE_MODEL] = self.model
        if self.input_tokens is not None:
            attrs[GenAI.USAGE_INPUT_TOKENS] = self.input_tokens
        if self.output_tokens is not None:
            attrs[GenAI.USAGE_OUTPUT_TOKENS] = self.output_tokens
        if self.cached_input_tokens is not None:
            attrs[GenAI.USAGE_CACHED_INPUT_TOKENS] = self.cached_input_tokens
        if self.tool_calls:
            first = self.tool_calls[0]
            if first.name is not None:
                attrs[GenAI.TOOL_NAME] = first.name
            if first.call_id is not None:
                attrs[GenAI.TOOL_CALL_ID] = first.call_id
        return attrs


class StreamReconstructor:
    """Incrementally reconstructs an OpenAI SSE completion. Never raises on malformed input.

    Feed raw SSE — whole chunks (:meth:`feed`) or single decoded lines (:meth:`feed_line`) — then
    call :meth:`result`. Honours the SDK's never-crash invariant: a malformed frame is skipped, not
    raised. Mark a transport drop with :meth:`mark_dropped` so the result is classified PARTIAL.
    """

    def __init__(self, *, token_estimator: Callable[[str], int] = _heuristic_output_tokens) -> None:
        self._estimator = token_estimator
        self._buf = ""
        self._model: str | None = None
        self._content: list[str] = []
        self._finish_reason: str | None = None
        self._tools: dict[int, ReconstructedToolCall] = {}
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._cached_tokens: int | None = None
        self._usage_from_provider = False
        self._saw_done = False
        self._dropped = False

    # --- feeding ---------------------------------------------------------------------------------

    def feed(self, chunk: str | bytes) -> None:
        """Feed a raw SSE chunk (may contain zero or more complete ``data:`` frames)."""
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self._buf += chunk
        # SSE events are separated by a blank line; process complete events, keep the remainder.
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.feed_line(line)

    def feed_line(self, line: str) -> None:
        """Feed one decoded SSE line. Non-data lines (comments, blanks) are ignored."""
        line = line.strip()
        if not line or not line.startswith(_DATA_PREFIX):
            return
        payload = line[len(_DATA_PREFIX):].strip()
        if payload == _DONE:
            self._saw_done = True
            return
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return  # never crash on a malformed frame
        if isinstance(obj, dict):
            self._consume(obj)

    def mark_dropped(self) -> None:
        """Signal the upstream connection dropped (200 then cut) — forces PARTIAL classification."""
        self._dropped = True

    # --- parsing ---------------------------------------------------------------------------------

    def _consume(self, obj: dict[str, object]) -> None:
        model = obj.get("model")
        if isinstance(model, str) and model:
            self._model = model

        choices = obj.get("choices")
        if isinstance(choices, (list, tuple)):
            for choice in choices:
                if isinstance(choice, dict):
                    self._consume_choice(choice)

        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._consume_usage(usage)

    def _consume_choice(self, choice: dict[str, object]) -> None:
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self._finish_reason = finish

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        content = delta.get("content")
        if isinstance(content, str):
            self._content.append(content)

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    self._consume_tool_delta(tc)

    def _consume_tool_delta(self, tc: dict[str, object]) -> None:
        index = tc.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = 0
        call = self._tools.get(index)
        if call is None:
            call = ReconstructedToolCall(index=index)
            self._tools[index] = call
        call_id = tc.get("id")
        if isinstance(call_id, str) and call_id:
            call.call_id = call_id
        fn = tc.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                call.name = name
            args = fn.get("arguments")
            if isinstance(args, str):
                call.arguments += args

    def _consume_usage(self, usage: dict[str, object]) -> None:
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            self._input_tokens = prompt
        completion = usage.get("completion_tokens")
        if isinstance(completion, int) and not isinstance(completion, bool):
            self._output_tokens = completion
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
            if isinstance(cached, int) and not isinstance(cached, bool):
                self._cached_tokens = cached
        self._usage_from_provider = True

    # --- result ----------------------------------------------------------------------------------

    def _is_complete(self) -> bool:
        if self._dropped:
            return False
        return self._saw_done or self._usage_from_provider or self._finish_reason is not None

    def result(self) -> StreamResult:
        """Reconstruct the final :class:`StreamResult` from everything fed so far."""
        # flush any trailing buffered line (a final frame without a trailing newline)
        if self._buf.strip():
            tail, self._buf = self._buf, ""
            self.feed_line(tail)

        content = "".join(self._content)
        complete = self._is_complete()
        output_tokens = self._output_tokens
        usage_from_provider = self._usage_from_provider
        if output_tokens is None:
            # No provider usage (typical on a mid-stream drop): estimate from received content so
            # cost reflects what the customer actually received.
            output_tokens = self._estimator(content)
            usage_from_provider = False

        tools = [self._tools[i] for i in sorted(self._tools)]
        return StreamResult(
            status=StreamStatus.COMPLETE if complete else StreamStatus.PARTIAL,
            model=self._model,
            content=content,
            finish_reason=self._finish_reason,
            tool_calls=tools,
            input_tokens=self._input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=self._cached_tokens,
            usage_from_provider=usage_from_provider,
        )


def tee(
    upstream: Iterable[str | bytes],
    reconstructor: StreamReconstructor,
    *,
    on_drop: bool = True,
) -> Iterator[str | bytes]:
    """Forward ``upstream`` chunks to the client unchanged while copying them to ``reconstructor``.

    Each chunk is yielded to the client **before** it is fed to the reconstructor, so the client
    path is never delayed by reconstruction work. If iterating ``upstream`` raises (a mid-stream
    transport drop) and ``on_drop`` is True, the reconstructor is marked dropped (→ PARTIAL) and
    the exception is re-raised so the caller can propagate the broken stream.
    """
    try:
        for chunk in upstream:
            yield chunk  # client path first — zero added latency
            reconstructor.feed(chunk)
    except Exception:
        if on_drop:
            reconstructor.mark_dropped()
        raise


def reconstruct(
    sse: Iterable[str | bytes] | str | bytes,
    *,
    dropped: bool = False,
    token_estimator: Callable[[str], int] = _heuristic_output_tokens,
) -> StreamResult:
    """Convenience: reconstruct a whole SSE stream (iterable of chunks, or one blob) in one call."""
    rec = StreamReconstructor(token_estimator=token_estimator)
    if isinstance(sse, (str, bytes)):
        rec.feed(sse)
    else:
        for chunk in sse:
            rec.feed(chunk)
    if dropped:
        rec.mark_dropped()
    return rec.result()
