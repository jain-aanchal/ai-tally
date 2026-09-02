# SPDX-License-Identifier: Apache-2.0
"""Shared instrumentation machinery: provider interface, span builder, call wrappers.

The sync :func:`wrap_create` (CTO-48) is unchanged. Initiative 2 (CTO-260) adds the async and
streaming wrapper variants the one-line ``tally.init`` path needs, plus two optional hooks on the
:class:`ProviderInstrumentor` protocol:

- ``operation`` names the ``gen_ai.operation.name`` bucket (default ``"chat"``), so the same
  machinery serves embeddings and the Responses API without a per-op fork.
- ``compute_cost`` lets a provider price its own operation (embeddings price under
  ``PriceType.EMBEDDING``, not the chat input/output rates); when absent, the chat pricing path is
  used.

Two invariants ride through every wrapper here and are not negotiable (CLAUDE.md):

- The provider call runs OUTSIDE the safety boundary, so the customer's real API error propagates
  unchanged and instrumentation can never swallow it.
- Only span building + hand-off run inside ``safe_block``, so a bug in extraction, pricing, or the
  transport can never raise into the caller.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from datetime import date
from typing import Protocol

from tally.context import current_context
from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd
from tally.safety import SelfObservability, safe_block
from tally.schema import SpanFields, build_span_attributes

#: Resolves the active account dimension for a span: ``(hash, key_version, label)``. Wired to
#: ``TallyClient._resolve_account`` by ``tally.init`` so an auto-instrumented span carries the same
#: HMAC'd account the manual ``record_*`` path would (CTO-260 §4.4). Returns all-``None`` when no
#: account is in scope or it cannot be hashed (honest unattributed, never a raw id).
AccountResolver = Callable[[], tuple[str | None, str | None, str | None]]


class ProviderInstrumentor(Protocol):
    """Per-provider knowledge. Pure functions over a provider response object.

    ``extract_usage`` may return ``None`` to signal usage is genuinely unknown (a streamed call
    that carried no terminal usage event): the span is then emitted with null token counts rather
    than a fabricated zero, per "honest under uncertainty".
    """

    system: str

    def request_model(self, args: tuple, kwargs: dict) -> str | None: ...
    def response_model(self, response: object) -> str | None: ...
    def extract_usage(self, response: object) -> Usage | None: ...


def _instrumentor_operation(instrumentor: ProviderInstrumentor) -> str:
    return getattr(instrumentor, "operation", "chat")


def build_span(
    instrumentor: ProviderInstrumentor,
    *,
    args: tuple,
    kwargs: dict,
    response: object,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    at: date | None = None,
    account: tuple[str | None, str | None, str | None] | None = None,
) -> dict[str, object]:
    """Build a conformant span attribute dict from a provider response.

    Pulls feature_tag/session from the active trace context, computes cost from the catalog (if
    given and usage is known), stamps the account dimension (if ``account`` is supplied), and
    returns attributes guaranteed to pass ``validate_span_attributes``.

    When ``instrumentor.extract_usage`` returns ``None`` the token counts are left null and no cost
    is computed: an honest blank, never a guessed zero (CTO-260 §4.3).
    """
    usage = instrumentor.extract_usage(response)
    req_model = instrumentor.request_model(args, kwargs)
    resp_model = instrumentor.response_model(response) or req_model
    operation = _instrumentor_operation(instrumentor)
    ctx = current_context()

    cost_micro: int | None = None
    catalog_version: str | None = None
    if catalog is not None and resp_model and usage is not None:
        cost_fn = getattr(instrumentor, "compute_cost", None)
        if cost_fn is not None:
            cost_micro, version = cost_fn(
                catalog, resp_model, usage, at=at, tenant_id=tenant_id
            )
        else:
            cost_micro, version = compute_cost_micro_usd(
                catalog, instrumentor.system, resp_model, usage, at=at, tenant_id=tenant_id
            )
        catalog_version = version or None

    acct_hash, acct_version, acct_label = account or (None, None, None)

    fields = SpanFields(
        system=instrumentor.system,
        request_model=req_model,
        response_model=resp_model,
        operation=operation,
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        cached_input_tokens=None if usage is None else (usage.cached_input_tokens or None),
        cost_estimated_micro_usd=cost_micro,
        price_catalog_version=catalog_version,
        feature_tag=ctx.feature_tag,
        session_id=ctx.session_id,
        account_id_hash=acct_hash,
        account_id_hash_key_version=acct_version,
        account_label=acct_label,
    )
    return build_span_attributes(fields)


def _resolve_account(account_resolver: AccountResolver | None):
    if account_resolver is None:
        return None
    return account_resolver()


def wrap_create(
    create_fn: Callable[..., object],
    instrumentor: ProviderInstrumentor,
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
) -> Callable[..., object]:
    """Wrap a sync provider ``create``-style callable so each successful call emits a span.

    The provider call itself is NOT guarded - its exceptions propagate to the caller unchanged.
    Only span building + ``on_span`` run inside the safety boundary, so instrumentation can never
    break the customer's call.
    """
    observ = obs or SelfObservability()

    @functools.wraps(create_fn)
    def wrapper(*args, **kwargs):
        response = create_fn(*args, **kwargs)  # provider errors propagate - by design
        with safe_block(observ, where=f"instrument.{instrumentor.system}"):
            attrs = build_span(
                instrumentor,
                args=args,
                kwargs=kwargs,
                response=response,
                catalog=catalog,
                tenant_id=tenant_id,
                account=_resolve_account(account_resolver),
            )
            on_span(attrs)
        return response

    return wrapper


def wrap_create_async(
    create_fn: Callable[..., object],
    instrumentor: ProviderInstrumentor,
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
) -> Callable[..., object]:
    """Async variant of :func:`wrap_create` for ``AsyncOpenAI`` / ``AsyncAnthropic`` (CTO-260 §4.2).

    Only awaits the customer's coroutine; span building + hand-off never await the transport, which
    enqueues without blocking (CTO-260 §5).
    """
    observ = obs or SelfObservability()

    @functools.wraps(create_fn)
    async def wrapper(*args, **kwargs):
        response = await create_fn(*args, **kwargs)  # provider errors propagate - by design
        with safe_block(observ, where=f"instrument.{instrumentor.system}"):
            attrs = build_span(
                instrumentor,
                args=args,
                kwargs=kwargs,
                response=response,
                catalog=catalog,
                tenant_id=tenant_id,
                account=_resolve_account(account_resolver),
            )
            on_span(attrs)
        return response

    return wrapper


class StreamInstrumentor(Protocol):
    """A :class:`ProviderInstrumentor` that also accumulates usage across a streamed response.

    ``accumulate`` folds one chunk/event into an opaque per-stream ``state`` dict (never shared,
    so concurrent streams cannot cross-contaminate). ``finalize`` reads the accumulated state and
    returns ``(model, usage)`` - ``usage`` is ``None`` when the stream carried no terminal usage
    event, which yields an honest null-token span rather than a fabricated zero (CTO-260 §4.3).
    """

    system: str

    def request_model(self, args: tuple, kwargs: dict) -> str | None: ...
    def accumulate(self, state: dict, chunk: object) -> None: ...
    def finalize(self, state: dict) -> tuple[str | None, Usage | None]: ...


def _emit_stream_span(
    instrumentor: StreamInstrumentor,
    *,
    args: tuple,
    kwargs: dict,
    state: dict,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability,
    catalog: PriceCatalog | None,
    tenant_id: str | None,
    account_resolver: AccountResolver | None,
) -> None:
    """Build + hand off the terminal span for an exhausted stream. Never raises."""
    with safe_block(obs, where=f"instrument.{instrumentor.system}.stream"):
        resp_model, usage = instrumentor.finalize(state)
        req_model = instrumentor.request_model(args, kwargs)
        resp_model = resp_model or req_model
        operation = _instrumentor_operation(instrumentor)
        ctx = current_context()

        cost_micro: int | None = None
        catalog_version: str | None = None
        if catalog is not None and resp_model and usage is not None:
            cost_micro, version = compute_cost_micro_usd(
                catalog, instrumentor.system, resp_model, usage, tenant_id=tenant_id
            )
            catalog_version = version or None

        acct_hash, acct_version, acct_label = _resolve_account(account_resolver) or (
            None,
            None,
            None,
        )
        fields = SpanFields(
            system=instrumentor.system,
            request_model=req_model,
            response_model=resp_model,
            operation=operation,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            cached_input_tokens=None if usage is None else (usage.cached_input_tokens or None),
            cost_estimated_micro_usd=cost_micro,
            price_catalog_version=catalog_version,
            feature_tag=ctx.feature_tag,
            session_id=ctx.session_id,
            account_id_hash=acct_hash,
            account_id_hash_key_version=acct_version,
            account_label=acct_label,
        )
        if usage is None:
            # Honest blank, not a silent drop: record why tokens are null so the SDK's
            # self-observability can surface "streamed without usage" (CTO-260 §4.3).
            obs.last_errors.append(
                f"instrument.{instrumentor.system}.stream: no usage on stream (tokens null)"
            )
            if len(obs.last_errors) > obs._max_errors:
                del obs.last_errors[0]
        on_span(build_span_attributes(fields))


class _InstrumentedStream:
    """Pass-through wrapper over a provider sync ``Stream`` object (CTO-260 §4.3, review finding).

    Yields each chunk untouched while folding usage/model, and emits the terminal span exactly once
    (when iteration is exhausted or the ``with`` block exits, whichever comes first). Crucially it
    preserves the underlying object: attribute access is delegated (``.response``, ``.close()``) and
    the context-manager protocol is honored, so ``with client.chat.completions.create(stream=True)
    as s:`` and ``s.response`` keep working instead of breaking on a bare generator.
    """

    def __init__(self, raw, instrumentor, emit):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_instr", instrumentor)
        object.__setattr__(self, "_emit", emit)
        object.__setattr__(self, "_state", {})
        object.__setattr__(self, "_emitted", False)

    def _emit_once(self):
        if not object.__getattribute__(self, "_emitted"):
            object.__setattr__(self, "_emitted", True)
            object.__getattribute__(self, "_emit")(object.__getattribute__(self, "_state"))

    def __iter__(self):
        instr = object.__getattribute__(self, "_instr")
        state = object.__getattribute__(self, "_state")
        try:
            for chunk in object.__getattribute__(self, "_raw"):
                _safe_accumulate(instr, state, chunk)
                yield chunk
        finally:
            self._emit_once()

    def __enter__(self):
        enter = getattr(object.__getattribute__(self, "_raw"), "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc):
        try:
            raw = object.__getattribute__(self, "_raw")
            exit_ = getattr(raw, "__exit__", None)
            if callable(exit_):
                return exit_(*exc)
            close = getattr(raw, "close", None)
            if callable(close):
                close()
            return False
        finally:
            self._emit_once()

    def __getattr__(self, name):
        # Delegate everything else (response, close, ...) to the real Stream object.
        return getattr(object.__getattribute__(self, "_raw"), name)


class _InstrumentedAsyncStream:
    """Async counterpart of :class:`_InstrumentedStream`, preserving the async ``Stream`` object."""

    def __init__(self, raw, instrumentor, emit):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_instr", instrumentor)
        object.__setattr__(self, "_emit", emit)
        object.__setattr__(self, "_state", {})
        object.__setattr__(self, "_emitted", False)

    def _emit_once(self):
        if not object.__getattribute__(self, "_emitted"):
            object.__setattr__(self, "_emitted", True)
            object.__getattribute__(self, "_emit")(object.__getattribute__(self, "_state"))

    async def __aiter__(self):
        instr = object.__getattribute__(self, "_instr")
        state = object.__getattribute__(self, "_state")
        try:
            async for chunk in object.__getattribute__(self, "_raw"):
                _safe_accumulate(instr, state, chunk)
                yield chunk
        finally:
            self._emit_once()

    async def __aenter__(self):
        aenter = getattr(object.__getattribute__(self, "_raw"), "__aenter__", None)
        if callable(aenter):
            await aenter()
        return self

    async def __aexit__(self, *exc):
        try:
            raw = object.__getattribute__(self, "_raw")
            aexit = getattr(raw, "__aexit__", None)
            if callable(aexit):
                return await aexit(*exc)
            aclose = getattr(raw, "aclose", None) or getattr(raw, "close", None)
            if callable(aclose):
                result = aclose()
                if inspect.isawaitable(result):
                    await result
            return False
        finally:
            self._emit_once()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)


def _safe_accumulate(instrumentor: StreamInstrumentor, state: dict, chunk: object) -> None:
    """Fold one chunk into per-stream state. Must never break the caller's iteration."""
    try:
        instrumentor.accumulate(state, chunk)
    except Exception:  # noqa: BLE001 - a bad chunk must never raise into the customer's loop
        pass


def wrap_stream(
    stream_fn: Callable[..., object],
    instrumentor: StreamInstrumentor,
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
) -> Callable[..., object]:
    """Wrap a sync streaming ``create(stream=True)`` call (CTO-260 §4.3).

    Returns a pass-through proxy that yields each chunk untouched and folds usage/model from the
    terminal event; the span is emitted once, when the stream is exhausted or its ``with`` block
    exits. The stream is never buffered or altered, and the underlying provider ``Stream`` object's
    attributes and context-manager protocol are preserved.
    """
    observ = obs or SelfObservability()

    @functools.wraps(stream_fn)
    def wrapper(*args, **kwargs):
        raw = stream_fn(*args, **kwargs)  # provider errors propagate - by design

        def _emit(state: dict) -> None:
            _emit_stream_span(
                instrumentor,
                args=args,
                kwargs=kwargs,
                state=state,
                on_span=on_span,
                obs=observ,
                catalog=catalog,
                tenant_id=tenant_id,
                account_resolver=account_resolver,
            )

        return _InstrumentedStream(raw, instrumentor, _emit)

    return wrapper


def wrap_stream_async(
    stream_fn: Callable[..., object],
    instrumentor: StreamInstrumentor,
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
) -> Callable[..., object]:
    """Async variant of :func:`wrap_stream` for an async streamed ``create(stream=True)``.

    The wrapped coroutine returns a pass-through proxy that yields each chunk untouched and emits
    the span once the async stream is exhausted or its ``async with`` block exits, preserving the
    underlying async ``Stream`` object's attributes and context-manager protocol.
    """
    observ = obs or SelfObservability()

    @functools.wraps(stream_fn)
    async def wrapper(*args, **kwargs):
        raw = await stream_fn(*args, **kwargs)  # provider errors propagate - by design

        def _emit(state: dict) -> None:
            _emit_stream_span(
                instrumentor,
                args=args,
                kwargs=kwargs,
                state=state,
                on_span=on_span,
                obs=observ,
                catalog=catalog,
                tenant_id=tenant_id,
                account_resolver=account_resolver,
            )

        return _InstrumentedAsyncStream(raw, instrumentor, _emit)

    return wrapper
