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
- :func:`note_context_drop` — record that an expected context was missing (feeds
  ``SelfObservability.context_drop_count``).
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


def note_context_drop(obs: SelfObservability, *, where: str = "context") -> None:
    """Record that a span was produced with no active trace context where one was expected."""
    obs.context_drop_count += 1
    obs.last_errors.append(f"{where}: context drop (no active trace_id)")
    if len(obs.last_errors) > obs._max_errors:
        del obs.last_errors[0]
