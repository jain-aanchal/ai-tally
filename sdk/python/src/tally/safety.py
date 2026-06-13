# SPDX-License-Identifier: Apache-2.0
"""SDK safety boundary — the never-crash-host invariant.

THE INVARIANT (CTO-45): the SDK must never raise into the customer's code path. Every internal
operation runs inside a boundary that catches *all* exceptions (including ``BaseException``
subclasses we can safely swallow — but never ``KeyboardInterrupt`` / ``SystemExit``), records them
to a self-observability channel, and returns a fallback so the customer's call proceeds unmodified.

Usage:

    from tally.safety import SelfObservability, safe, safe_block

    obs = SelfObservability()

    @safe(obs, fallback=None)
    def _internal(...): ...

    with safe_block(obs):
        ... # best-effort internal work; exceptions recorded, never raised
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TypeVar

_log = logging.getLogger("tally")

T = TypeVar("T")

# We swallow Exception, never these (let the program exit/interrupt as the user intends).
_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit)


@dataclass(slots=True)
class SelfObservability:
    """Counters the SDK reports about *itself* (surfaced to the customer's dashboard).

    See CTO-46 for ``context_drop_count`` and CTO-49 for ``dropped_span_count``; this module owns
    ``internal_error_count`` and the optional error hook.
    """

    internal_error_count: int = 0
    context_drop_count: int = 0
    dropped_span_count: int = 0
    #: optional sink for the last few error reprs (bounded), for debugging
    last_errors: list[str] = field(default_factory=list)
    _max_errors: int = 20

    def record_error(self, exc: BaseException, where: str) -> None:
        self.internal_error_count += 1
        msg = f"{where}: {type(exc).__name__}: {exc}"
        self.last_errors.append(msg)
        if len(self.last_errors) > self._max_errors:
            del self.last_errors[0]
        # Never propagate; log at debug so we don't spam the customer's logs by default.
        _log.debug("tally internal error suppressed (%s)", msg)

    def snapshot(self) -> dict[str, int]:
        return {
            "internal_error_count": self.internal_error_count,
            "context_drop_count": self.context_drop_count,
            "dropped_span_count": self.dropped_span_count,
        }


def safe(obs: SelfObservability, *, fallback: T = None, where: str | None = None):
    """Decorator: run the wrapped callable inside the safety boundary.

    On any swallowable exception, record it and return ``fallback`` instead of raising.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        label = where or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            try:
                return fn(*args, **kwargs)
            except _NEVER_SWALLOW:
                raise
            except BaseException as exc:  # noqa: BLE001 - intentional catch-all boundary
                obs.record_error(exc, label)
                return fallback

        return wrapper

    return decorator


@contextmanager
def safe_block(obs: SelfObservability, *, where: str = "safe_block") -> Iterator[None]:
    """Context manager form of :func:`safe`.

    Exceptions inside the block are recorded, not raised.
    """
    try:
        yield
    except _NEVER_SWALLOW:
        raise
    except BaseException as exc:  # noqa: BLE001 - intentional catch-all boundary
        obs.record_error(exc, where)
