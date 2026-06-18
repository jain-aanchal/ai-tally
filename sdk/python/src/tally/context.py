# SPDX-License-Identifier: Apache-2.0
"""Context propagation — trace_id + feature_tag that survive async boundaries.

The make-or-break piece (CTO-46): if context is lost across an ``await`` / thread / background
task, attribution and agent trees break. We use :mod:`contextvars`, which propagate correctly
across ``asyncio`` tasks (each task copies the current context) and stay isolated across threads.

Public surface:

- :func:`start_trace` — begin a new trace context (generates a trace_id).
- :func:`with_trace_context` — context manager to set/restore explicitly (the escape hatch for
  places where automatic propagation fails: Celery, Temporal, Lambda cold starts, etc.).
- :func:`current_context` — read the active context.
- :func:`note_context_drop` — two modes (CTO-118):
    (a) record that an expected trace context was missing (feeds
        ``SelfObservability.context_drop_count``); or
    (b) emit ``gen_ai.context.*`` span attributes describing how many messages /
        tokens were trimmed to fit the model's context window. Counts only — never
        the dropped message text.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tally.safety import SelfObservability

_trace_id: ContextVar[str | None] = ContextVar("tally_trace_id", default=None)
_feature_tag: ContextVar[str | None] = ContextVar("tally_feature_tag", default=None)
_session_id: ContextVar[str | None] = ContextVar("tally_session_id", default=None)


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str | None
    feature_tag: str | None
    session_id: str | None

    @property
    def is_active(self) -> bool:
        return self.trace_id is not None


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_context() -> TraceContext:
    """Snapshot the active context (may be inactive)."""
    return TraceContext(_trace_id.get(), _feature_tag.get(), _session_id.get())


@contextmanager
def with_trace_context(
    *,
    trace_id: str | None = None,
    feature_tag: str | None = None,
    session_id: str | None = None,
    inherit: bool = True,
) -> Iterator[TraceContext]:
    """Set the trace context for the duration of the block, then restore prior values.

    This is both the normal entrypoint and the manual escape hatch when automatic propagation
    can't carry context (e.g. across a process/queue boundary — re-establish it on the far side).

    Args:
        trace_id: explicit id; if ``None`` and no active trace, a new one is generated.
        feature_tag / session_id: optional tags.
        inherit: when True, unset fields fall back to the currently-active context.
    """
    cur = current_context()
    resolved_trace = trace_id or (cur.trace_id if inherit else None) or new_trace_id()
    resolved_feature = feature_tag or (cur.feature_tag if inherit else None)
    resolved_session = session_id or (cur.session_id if inherit else None)

    t_tok = _trace_id.set(resolved_trace)
    f_tok = _feature_tag.set(resolved_feature)
    s_tok = _session_id.set(resolved_session)
    try:
        yield TraceContext(resolved_trace, resolved_feature, resolved_session)
    finally:
        _trace_id.reset(t_tok)
        _feature_tag.reset(f_tok)
        _session_id.reset(s_tok)


def start_trace(
    *, feature_tag: str | None = None, session_id: str | None = None
) -> AbstractContextManager[TraceContext]:
    """Begin a fresh trace (always a new trace_id). Returns the context manager."""
    return with_trace_context(
        trace_id=new_trace_id(),
        feature_tag=feature_tag,
        session_id=session_id,
        inherit=False,
    )


def note_context_drop(
    obs: SelfObservability,
    *,
    where: str = "context",
    dropped_messages: int | None = None,
    dropped_tokens: int | None = None,
    window_used_pct: float | None = None,
    attrs: dict[str, object] | None = None,
) -> dict[str, object]:
    """Record a context drop and (optionally) emit span attributes describing the drop.

    Two related signals share this entrypoint (CTO-118):

    1. **Trace-context drop** — no active ``trace_id`` where one was expected. The existing
       no-arg call path (``note_context_drop(obs, where="record_llm_call")``) keeps working
       and bumps :attr:`SelfObservability.context_drop_count`.

    2. **Context-window drop** — caller trimmed messages before sending to the model to
       fit the context window. Pass ``dropped_messages`` (count), ``dropped_tokens``
       (total tokens of trimmed content), and ``window_used_pct`` (0..1 — how close the
       request got to the window). These are promoted to three span attributes:

       - ``gen_ai.context.dropped_messages`` (int)
       - ``gen_ai.context.dropped_tokens`` (int)
       - ``gen_ai.context.window_used_pct`` (float)

       Counts only — never the dropped message text. This is the contract.

    Args:
        obs: self-observability sink for the trace-drop counter.
        where: caller identifier (used in ``last_errors`` for trace-drop case).
        dropped_messages: count of messages trimmed before send. Negative → clamped to 0.
        dropped_tokens: total token count of trimmed content. Negative → clamped to 0.
        window_used_pct: 0..1 fraction of the model's context window used. Clamped to [0, 1].
        attrs: an optional attribute dict to mutate in place. If provided, the three
            context-drop attributes are added to it. A new dict is also returned either way.

    Returns:
        The (possibly mutated) attribute dict. Empty when no drop fields were provided.
    """
    out = attrs if attrs is not None else {}
    if dropped_messages is None and dropped_tokens is None and window_used_pct is None:
        # Legacy trace-context drop path — unchanged behaviour.
        obs.context_drop_count += 1
        obs.last_errors.append(f"{where}: context drop (no active trace_id)")
        if len(obs.last_errors) > obs._max_errors:
            del obs.last_errors[0]
        return out

    # Context-window drop path. Normalize: never trust the caller blindly.
    if dropped_messages is not None:
        out["gen_ai.context.dropped_messages"] = max(0, int(dropped_messages))
    if dropped_tokens is not None:
        out["gen_ai.context.dropped_tokens"] = max(0, int(dropped_tokens))
    if window_used_pct is not None:
        pct = float(window_used_pct)
        if pct < 0.0:
            pct = 0.0
        elif pct > 1.0:
            pct = 1.0
        out["gen_ai.context.window_used_pct"] = pct
    return out
