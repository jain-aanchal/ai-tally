# SPDX-License-Identifier: Apache-2.0
"""Monkeypatch the official ``openai`` / ``anthropic`` clients in place (CTO-260 §4).

``tally.init(instrument=True)`` calls :func:`patch_openai` / :func:`patch_anthropic`. Each patches
provider methods at the CLASS / unbound-method level, so every client instance (including ones
constructed after ``init``) is covered, and stores a handle so :func:`unpatch_all` can reverse it
(tests use this to avoid cross-test leakage). Patching is:

- **Never a hard dependency.** Nothing here imports ``openai`` / ``anthropic`` unless it is already
  installed; a missing library is a no-op, not an error (the SDK keeps zero required runtime deps).
- **Idempotent.** Each wrapper is tagged with a sentinel attribute; a second patch is skipped.
- **Never-raise.** The provider call runs outside the safety boundary (see
  :mod:`tally.instrumentation.base`); resolution and patching here are wrapped so a shape change in
  a future provider release degrades to "not instrumented", never a crash into ``init``.

Tests inject fake provider classes via the ``targets`` argument, so the wrappers can be exercised
without installing ``openai`` / ``anthropic`` or hitting the network.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from tally.instrumentation.anthropic import AnthropicInstrumentor
from tally.instrumentation.base import (
    AccountResolver,
    wrap_create,
    wrap_create_async,
    wrap_stream,
    wrap_stream_async,
)
from tally.instrumentation.openai import (
    OpenAIEmbeddingsInstrumentor,
    OpenAIInstrumentor,
    OpenAIResponsesInstrumentor,
)
from tally.pricing import PriceCatalog
from tally.safety import SelfObservability, safe_block

_log = logging.getLogger("tally")

_SENTINEL = "_tally_wrapped"

# (cls, attr, original) so uninstrument restores the exact prior attribute. Process-global, like
# the patch itself; ``init`` is single-tenant per process by design (CTO-260 §3).
_PATCHES: list[tuple[type, str, object]] = []


@dataclass(frozen=True, slots=True)
class _Common:
    on_span: Callable[[dict[str, object]], None]
    obs: SelfObservability
    catalog: PriceCatalog | None
    tenant_id: str | None
    account_resolver: AccountResolver | None

    def kw(self) -> dict:
        return {
            "on_span": self.on_span,
            "obs": self.obs,
            "catalog": self.catalog,
            "tenant_id": self.tenant_id,
            "account_resolver": self.account_resolver,
        }


def _resolve(module_path: str, class_name: str) -> type | None:
    """Import ``module_path`` and return its ``class_name``, or ``None`` if unavailable."""
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - a missing provider lib is a no-op, not an error
        return None
    return getattr(module, class_name, None)


def _apply(cls: type | None, attr: str, builder: Callable[[object], object]) -> None:
    """Replace ``cls.attr`` with ``builder(original)``, idempotent and reversible. Never raises."""
    if cls is None:
        return
    original = getattr(cls, attr, None)
    if original is None:
        return
    if getattr(original, _SENTINEL, False):
        return  # already patched — idempotent
    wrapped = builder(original)
    try:
        setattr(wrapped, _SENTINEL, True)
        wrapped._tally_original = original
    except (AttributeError, TypeError):
        pass
    setattr(cls, attr, wrapped)
    _PATCHES.append((cls, attr, original))


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
def _openai_chat_builder(common: _Common, *, is_async: bool, stream_usage: bool):
    instr = OpenAIInstrumentor()

    def builder(original):
        if is_async:
            nonstream = wrap_create_async(original, instr, **common.kw())
            streamed = wrap_stream_async(original, instr, **common.kw())

            @functools.wraps(original)
            async def wrapper(*args, **kwargs):
                if kwargs.get("stream"):
                    if stream_usage and "stream_options" not in kwargs:
                        kwargs = {**kwargs, "stream_options": {"include_usage": True}}
                    return await streamed(*args, **kwargs)
                return await nonstream(*args, **kwargs)

            return wrapper

        nonstream = wrap_create(original, instr, **common.kw())
        streamed = wrap_stream(original, instr, **common.kw())

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            if kwargs.get("stream"):
                if stream_usage and "stream_options" not in kwargs:
                    kwargs = {**kwargs, "stream_options": {"include_usage": True}}
                return streamed(*args, **kwargs)
            return nonstream(*args, **kwargs)

        return wrapper

    return builder


def _plain_builder(common: _Common, instrumentor, *, is_async: bool):
    def builder(original):
        if is_async:
            return wrap_create_async(original, instrumentor, **common.kw())
        return wrap_create(original, instrumentor, **common.kw())

    return builder


def patch_openai(
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
    instrument_stream_usage: bool = False,
    targets: dict[str, type] | None = None,
) -> None:
    """Patch the installed ``openai`` client (Chat Completions, Responses, Embeddings).

    ``targets`` overrides class resolution for tests: a mapping with any of the roles ``chat``,
    ``chat_async``, ``responses``, ``responses_async``, ``embeddings``, ``embeddings_async`` to a
    class whose ``create`` method should be wrapped. When omitted, the real ``openai`` classes are
    resolved (and silently skipped if the library is not installed).
    """
    observ = obs or SelfObservability()
    common = _Common(on_span, observ, catalog, tenant_id, account_resolver)

    with safe_block(observ, where="patch_openai"):
        roles = targets or _openai_targets()
        if not roles:
            _log.debug("tally: openai not installed; skipping instrumentation")
            return

        for role, is_async in (("chat", False), ("chat_async", True)):
            _apply(
                roles.get(role),
                "create",
                _openai_chat_builder(
                    common, is_async=is_async, stream_usage=instrument_stream_usage
                ),
            )
        for role, is_async in (("responses", False), ("responses_async", True)):
            _apply(
                roles.get(role),
                "create",
                _plain_builder(common, OpenAIResponsesInstrumentor(), is_async=is_async),
            )
        for role, is_async in (("embeddings", False), ("embeddings_async", True)):
            _apply(
                roles.get(role),
                "create",
                _plain_builder(common, OpenAIEmbeddingsInstrumentor(), is_async=is_async),
            )


def _openai_targets() -> dict[str, type]:
    roles: dict[str, type] = {}
    candidates = {
        "chat": ("openai.resources.chat.completions", "Completions"),
        "chat_async": ("openai.resources.chat.completions", "AsyncCompletions"),
        "responses": ("openai.resources.responses", "Responses"),
        "responses_async": ("openai.resources.responses", "AsyncResponses"),
        "embeddings": ("openai.resources.embeddings", "Embeddings"),
        "embeddings_async": ("openai.resources.embeddings", "AsyncEmbeddings"),
    }
    for role, (module_path, class_name) in candidates.items():
        cls = _resolve(module_path, class_name)
        if cls is not None:
            roles[role] = cls
    return roles


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
class _StreamProxy:
    """Wraps the object a stream context manager yields, folding usage as events pass through."""

    def __init__(self, inner: object, instrumentor: AnthropicInstrumentor, state: dict) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_instr", instrumentor)
        object.__setattr__(self, "_state", state)

    def __iter__(self):
        for event in self._inner:
            _safe_accumulate(self._instr, self._state, event)
            yield event

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        async for event in self._inner:  # type: ignore[attr-defined]
            _safe_accumulate(self._instr, self._state, event)
            yield event

    def __getattr__(self, name: str):
        # Delegate everything else (text_stream, get_final_message, ...) to the real stream.
        return getattr(object.__getattribute__(self, "_inner"), name)


def _safe_accumulate(instr: AnthropicInstrumentor, state: dict, event: object) -> None:
    try:
        instr.accumulate(state, event)
    except Exception:  # noqa: BLE001 - accumulation must never break the customer's iteration
        pass


class _StreamManagerProxy:
    """Pass-through for ``client.messages.stream(...)``: emits one span when the block exits."""

    def __init__(
        self,
        manager: object,
        instrumentor: AnthropicInstrumentor,
        args: tuple,
        kwargs: dict,
        common: _Common,
    ) -> None:
        self._manager = manager
        self._instr = instrumentor
        self._args = args
        self._kwargs = kwargs
        self._common = common
        self._state: dict = {}

    def __enter__(self):
        inner = self._manager.__enter__()
        return _StreamProxy(inner, self._instr, self._state)

    def __exit__(self, *exc):
        result = self._manager.__exit__(*exc)
        self._emit()
        return result

    async def __aenter__(self):
        inner = await self._manager.__aenter__()
        return _StreamProxy(inner, self._instr, self._state)

    async def __aexit__(self, *exc):
        result = await self._manager.__aexit__(*exc)
        self._emit()
        return result

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_manager"), name)

    def _emit(self) -> None:
        from tally.instrumentation.base import _emit_stream_span

        # If the caller never iterated events, try the authoritative final message for usage.
        if "input_tokens" not in self._state and "output_tokens" not in self._state:
            with safe_block(self._common.obs, where="instrument.anthropic.stream.final"):
                get_final = getattr(self._manager, "get_final_message", None)
                if callable(get_final):
                    _safe_accumulate(self._instr, self._state, {"message": get_final()})
        _emit_stream_span(
            self._instr,
            args=self._args,
            kwargs=self._kwargs,
            state=self._state,
            on_span=self._common.on_span,
            obs=self._common.obs,
            catalog=self._common.catalog,
            tenant_id=self._common.tenant_id,
            account_resolver=self._common.account_resolver,
        )


def _anthropic_stream_builder(common: _Common):
    instr = AnthropicInstrumentor()

    def builder(original):
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            manager = original(*args, **kwargs)  # provider errors propagate — by design
            return _StreamManagerProxy(manager, instr, args, kwargs, common)

        return wrapper

    return builder


def patch_anthropic(
    *,
    on_span: Callable[[dict[str, object]], None],
    obs: SelfObservability | None = None,
    catalog: PriceCatalog | None = None,
    tenant_id: str | None = None,
    account_resolver: AccountResolver | None = None,
    targets: dict[str, type] | None = None,
) -> None:
    """Patch the installed ``anthropic`` client (Messages create + stream, sync + async).

    ``targets`` overrides class resolution for tests: a mapping with any of the roles ``messages``,
    ``messages_async`` (whose ``create`` and ``stream`` are wrapped). When omitted, the real
    ``anthropic`` classes are resolved (and silently skipped if the library is not installed).
    """
    observ = obs or SelfObservability()
    common = _Common(on_span, observ, catalog, tenant_id, account_resolver)
    instr = AnthropicInstrumentor()

    with safe_block(observ, where="patch_anthropic"):
        roles = targets or _anthropic_targets()
        if not roles:
            _log.debug("tally: anthropic not installed; skipping instrumentation")
            return

        _apply(roles.get("messages"), "create", _plain_builder(common, instr, is_async=False))
        _apply(
            roles.get("messages_async"),
            "create",
            _plain_builder(common, instr, is_async=True),
        )
        _apply(roles.get("messages"), "stream", _anthropic_stream_builder(common))
        _apply(roles.get("messages_async"), "stream", _anthropic_stream_builder(common))


def _anthropic_targets() -> dict[str, type]:
    roles: dict[str, type] = {}
    for role, class_name in (("messages", "Messages"), ("messages_async", "AsyncMessages")):
        cls = _resolve("anthropic.resources.messages", class_name) or _resolve(
            "anthropic.resources.messages.messages", class_name
        )
        if cls is not None:
            roles[role] = cls
    return roles


# --------------------------------------------------------------------------- #
# Reversal
# --------------------------------------------------------------------------- #
def unpatch_all() -> None:
    """Restore every patched method to its original. Idempotent; used by ``tally.uninstrument``."""
    while _PATCHES:
        cls, attr, original = _PATCHES.pop()
        try:
            setattr(cls, attr, original)
        except Exception:  # noqa: BLE001 - reversal is best-effort, never raises
            pass


def is_patched() -> bool:
    return bool(_PATCHES)
