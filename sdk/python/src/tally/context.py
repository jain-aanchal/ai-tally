# SPDX-License-Identifier: Apache-2.0
"""Context propagation - trace_id + feature_tag that survive async boundaries.

The make-or-break piece (CTO-46): if context is lost across an ``await`` / thread / background
task, attribution and agent trees break. We use :mod:`contextvars`, which propagate correctly
across ``asyncio`` tasks (each task copies the current context) and stay isolated across threads.

Public surface:

- :func:`start_trace` - begin a new trace context (generates a trace_id).
- :func:`with_trace_context` - context manager to set/restore explicitly (the escape hatch for
  places where automatic propagation fails: Celery, Temporal, Lambda cold starts, etc.).
- :func:`current_context` - read the active context.
- :func:`with_account` sets the tenant's own customer (``account_id``) for a block (CTO-181).
- :func:`note_context_drop` - two modes (CTO-118):
    (a) record that an expected trace context was missing (feeds
        ``SelfObservability.context_drop_count``); or
    (b) emit ``gen_ai.context.*`` span attributes describing how many messages /
        tokens were trimmed to fit the model's context window. Counts only - never
        the dropped message text.

The account dimension (CTO-181) rides here rather than on every call site. A web app knows which
customer a request belongs to once, at request start, so it sets it once and every span inside the
request picks it up; an individual call can still override it. The value held here is the RAW
account id, and it stays raw only in process memory: it is HMAC'd at emit time in
:class:`~tally.client.TallyClient` and only the digest plus its key version reach the wire.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tally.safety import SelfObservability

#: Sentinel for "the feature-tag contextvar was never set in this context", distinct from an
#: explicit ``None``. Lets a process default fill in for a thread that never ran ``init`` code while
#: an explicit ``start_trace(feature_tag=None)`` still reads back as ``None`` (CTO-260 §3).
_UNSET_FEATURE_TAG = "\x00__tally_unset__"

_trace_id: ContextVar[str | None] = ContextVar("tally_trace_id", default=None)
_feature_tag: ContextVar[str | None] = ContextVar("tally_feature_tag", default=_UNSET_FEATURE_TAG)
_session_id: ContextVar[str | None] = ContextVar("tally_session_id", default=None)

# Process-wide default feature tag set by ``tally.init(feature_tag=...)``. A ContextVar set in
# init's calling thread does not reach a gunicorn/Flask worker thread that started fresh, so the
# spans it emits carried feature_tag=None despite the docstring (review finding). This module-global
# is consulted at span build when the contextvar is unset, so the default reaches spans emitted from
# any thread. The contextvar still wins wherever it is set (explicit trace context overrides).
_process_default_feature_tag: str | None = None
# Raw account id + optional label (CTO-181). Never emitted raw; see the module docstring.
_account_id: ContextVar[str | None] = ContextVar("tally_account_id", default=None)
_account_label: ContextVar[str | None] = ContextVar("tally_account_label", default=None)


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str | None
    feature_tag: str | None
    session_id: str | None
    #: Raw account id (CTO-181). Hashed at emit time; never placed on a span as-is.
    account_id: str | None = None
    #: Optional human-readable account label. Wire-only, for the gateway's label store.
    account_label: str | None = None

    @property
    def is_active(self) -> bool:
        return self.trace_id is not None


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_default_feature_tag(feature_tag: str | None) -> None:
    """Set a process default feature tag for the current context (CTO-260 §3).

    ``tally.init(feature_tag=...)`` calls this so auto-instrumented spans carry a default tag when
    the caller has not opened an explicit :func:`start_trace`. It records a module-global process
    default (consulted at span build from any thread, including gunicorn/Flask workers that started
    after ``init``) and also sets the contextvar in the calling thread so asyncio tasks spawned from
    it copy it directly. An explicit ``start_trace`` / ``with_trace_context`` still overrides it for
    its scope.
    """
    global _process_default_feature_tag
    _process_default_feature_tag = feature_tag
    _feature_tag.set(feature_tag)


def _resolve_feature_tag() -> str | None:
    """The contextvar when it was set in this context, else the process default (CTO-260 §3)."""
    tag = _feature_tag.get()
    if tag is _UNSET_FEATURE_TAG:
        return _process_default_feature_tag
    return tag


def current_context() -> TraceContext:
    """Snapshot the active context (may be inactive)."""
    return TraceContext(
        _trace_id.get(),
        _resolve_feature_tag(),
        _session_id.get(),
        _account_id.get(),
        _account_label.get(),
    )


@contextmanager
def with_trace_context(
    *,
    trace_id: str | None = None,
    feature_tag: str | None = None,
    session_id: str | None = None,
    account_id: str | None = None,
    account_label: str | None = None,
    inherit: bool = True,
) -> Iterator[TraceContext]:
    """Set the trace context for the duration of the block, then restore prior values.

    This is both the normal entrypoint and the manual escape hatch when automatic propagation
    can't carry context (e.g. across a process/queue boundary - re-establish it on the far side).

    Args:
        trace_id: explicit id; if ``None`` and no active trace, a new one is generated.
        feature_tag / session_id: optional tags.
        account_id: raw id of the tenant's own customer (CTO-181). Hashed at emit time.
        account_label: optional display name for that account. Wire-only.
        inherit: when True, unset fields fall back to the currently-active context.
    """
    cur = current_context()
    resolved_trace = trace_id or (cur.trace_id if inherit else None) or new_trace_id()
    resolved_feature = feature_tag or (cur.feature_tag if inherit else None)
    resolved_session = session_id or (cur.session_id if inherit else None)
    resolved_account = account_id or (cur.account_id if inherit else None)
    resolved_label = account_label or (cur.account_label if inherit else None)

    t_tok = _trace_id.set(resolved_trace)
    f_tok = _feature_tag.set(resolved_feature)
    s_tok = _session_id.set(resolved_session)
    a_tok = _account_id.set(resolved_account)
    l_tok = _account_label.set(resolved_label)
    try:
        yield TraceContext(
            resolved_trace, resolved_feature, resolved_session, resolved_account, resolved_label
        )
    finally:
        _trace_id.reset(t_tok)
        _feature_tag.reset(f_tok)
        _session_id.reset(s_tok)
        _account_id.reset(a_tok)
        _account_label.reset(l_tok)


@contextmanager
def with_account(
    account_id: str | None,
    *,
    label: str | None = None,
) -> Iterator[TraceContext]:
    """Scope an account to a block without touching the trace (CTO-181).

    The intended shape for a web app: resolve the customer once in a request middleware, wrap the
    handler, and every span emitted inside the request carries the account. Nothing else about the
    context changes, so this composes with an already-active trace and with :func:`start_trace`
    in either order.

    ``account_id`` is the RAW id. It is HMAC'd per tenant at emit time and only the digest travels.
    Passing ``None`` clears the account for the block, which is how a background job that must not
    inherit a caller's account opts out.

    ``label`` is optional and is carried on the wire for the gateway to upsert into its label
    store. It is never written to the span row in ClickHouse, so a tenant that wants no customer
    names in the telemetry store simply omits it and everything still works off the hash.
    """
    cur = current_context()
    a_tok = _account_id.set(account_id)
    l_tok = _account_label.set(label if account_id else None)
    try:
        yield TraceContext(
            cur.trace_id,
            cur.feature_tag,
            cur.session_id,
            account_id,
            label if account_id else None,
        )
    finally:
        _account_id.reset(a_tok)
        _account_label.reset(l_tok)


def start_trace(
    *,
    feature_tag: str | None = None,
    session_id: str | None = None,
    account_id: str | None = None,
    account_label: str | None = None,
) -> AbstractContextManager[TraceContext]:
    """Begin a fresh trace (always a new trace_id). Returns the context manager.

    ``account_id`` / ``account_label`` are here for the common case where the trace and the
    account are both known at the same boundary (CTO-181). ``inherit=False`` means an outer
    account is deliberately not carried in: a fresh trace is a fresh attribution.
    """
    return with_trace_context(
        trace_id=new_trace_id(),
        feature_tag=feature_tag,
        session_id=session_id,
        account_id=account_id,
        account_label=account_label,
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

    1. **Trace-context drop** - no active ``trace_id`` where one was expected. The existing
       no-arg call path (``note_context_drop(obs, where="record_llm_call")``) keeps working
       and bumps :attr:`SelfObservability.context_drop_count`.

    2. **Context-window drop** - caller trimmed messages before sending to the model to
       fit the context window. Pass ``dropped_messages`` (count), ``dropped_tokens``
       (total tokens of trimmed content), and ``window_used_pct`` (0..1 - how close the
       request got to the window). These are promoted to three span attributes:

       - ``gen_ai.context.dropped_messages`` (int)
       - ``gen_ai.context.dropped_tokens`` (int)
       - ``gen_ai.context.window_used_pct`` (float)

       Counts only - never the dropped message text. This is the contract.

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
        # Legacy trace-context drop path - unchanged behaviour.
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
