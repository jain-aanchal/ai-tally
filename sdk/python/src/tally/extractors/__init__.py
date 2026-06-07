# SPDX-License-Identifier: Apache-2.0
"""Provider extractor framework — versioned, pluggable, fixture-tested.

Implements CTO-41.

An *extractor* turns a raw provider response (the OpenAI Chat Completions object, etc.) into a list
of conformant ``gen_ai.*`` attribute dicts (see :mod:`tally.schema`). It is deliberately narrower
than the instrumentation layer: pure logic over a response payload, no pricing, no context, no
network. This makes extractors trivially fixture-testable and lets us version provider quirks
independently of the rest of the SDK.

Design goals (per CTO-41):

* **Versioned** — each extractor carries an explicit version in its registry key (``"openai_v1"``),
  so a breaking change in a provider's response shape becomes ``"openai_v2"`` without disturbing
  callers pinned to v1.
* **Pluggable** — :class:`ProviderExtractor` is a structural :class:`~typing.Protocol`; new
  providers register via :func:`register` (or the :func:`extractor` decorator). Adding a provider
  requires *zero* changes to dispatch — :func:`get_extractor` is a pure registry lookup.
* **Never crash** — extractors honour the SDK's "never break the host app" invariant: malformed,
  missing, or wrongly-typed provider data yields whatever subset of attributes can be salvaged,
  never an exception.

A single response may yield multiple attribute dicts (e.g. one per tool call in a tool-calling
turn), so :meth:`ProviderExtractor.extract` returns a ``list[dict[str, object]]``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProviderExtractor(Protocol):
    """Per-provider, per-version response extractor.

    Implementations are pure functions over a provider response object. They MUST NOT raise on
    malformed input — return whatever subset of attributes is available instead.
    """

    #: Registry key carrying provider + version, e.g. ``"openai_v1"``.
    key: str

    def extract(self, response: object) -> list[dict[str, object]]:
        """Map a provider response to one or more conformant ``gen_ai.*`` attribute dicts."""
        ...


_REGISTRY: dict[str, ProviderExtractor] = {}


def register(extractor_obj: ProviderExtractor) -> ProviderExtractor:
    """Register ``extractor_obj`` under its ``.key``. Returns it (so it can be used as a value).

    Raises :class:`ValueError` on a missing/empty key or a duplicate registration — these are
    programmer errors at import time, not host-app runtime data, so failing loudly is correct.
    """
    key = getattr(extractor_obj, "key", None)
    if not isinstance(key, str) or not key:
        raise ValueError("extractor must have a non-empty str .key")
    if key in _REGISTRY:
        raise ValueError(f"extractor already registered for key {key!r}")
    _REGISTRY[key] = extractor_obj
    return extractor_obj


def extractor(cls: type) -> type:
    """Class decorator: instantiate ``cls`` and register the instance under its ``.key``."""
    register(cls())
    return cls


def get_extractor(provider_version: str) -> ProviderExtractor:
    """Look up a registered extractor by its versioned key (e.g. ``"openai_v1"``).

    Raises :class:`KeyError` for an unknown key (a configuration error, not host-app data).
    """
    try:
        return _REGISTRY[provider_version]
    except KeyError:
        raise KeyError(
            f"no extractor registered for {provider_version!r}; "
            f"known: {sorted(_REGISTRY)}"
        ) from None


def available_extractors() -> list[str]:
    """Return the sorted list of registered extractor keys."""
    return sorted(_REGISTRY)


# Importing the provider modules triggers their registration side effects. Keep these imports at the
# bottom so the registry primitives above are fully defined first.
from tally.extractors.openai import OpenAIExtractorV1  # noqa: E402

__all__ = [
    "ProviderExtractor",
    "register",
    "extractor",
    "get_extractor",
    "available_extractors",
    "OpenAIExtractorV1",
]
