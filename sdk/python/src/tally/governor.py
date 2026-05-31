"""Per-provider rate-limit governor for bulk replay.

Implements CTO-60. Spec §9 W1.

Bulk *replay* (re-running historical prompts to compare models/prompts) hammers provider APIs and
will hit rate limits. Two things must be true:

1. We must not get the tenant *banned* — so concurrency is capped **per provider** and retries are
   spread with exponential backoff + full jitter.
2. A 429 / throttled response is an artifact of *our* replay pressure, not of the prompt or model —
   so it must **not** contaminate latency or quality aggregates. Throttled outcomes are explicitly
   marked excluded.

This module is the *governing primitive* the replay execution engine (CTO-59, out of scope) will
call. It is a synchronous, in-memory state machine of integer counters — no network, no asyncio, no
real sleeping. ``time.sleep`` and ``random`` are **injected** so every decision is a pure, testable
function. A minimal :class:`threading.Lock` guards the mutable counters so the engine can drive it
from a thread pool, but the decision helpers (:func:`backoff_delay`, :func:`counts_toward_metrics`)
are pure and lock-free.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

#: Cap used when a provider has no explicit per-provider limit configured.
DEFAULT_MAX_CONCURRENCY = 8
#: Backoff schedule defaults (seconds). Base * 2**attempt, clamped to cap, then jittered.
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 60.0


class Outcome(str, Enum):
    """Lifecycle outcome of a single replay call. Closed set.

    ``THROTTLED`` and ``EXCLUDED`` are the outcomes that must never reach quality/latency
    aggregates — see :func:`counts_toward_metrics`.
    """

    ADMITTED = "admitted"  # passed the concurrency gate, now in flight
    COMPLETED = "completed"  # finished cleanly — counts toward metrics
    THROTTLED = "throttled"  # provider returned 429 / rate_limit — excluded from metrics
    EXCLUDED = "excluded"  # explicitly dropped for any non-quality reason — excluded


class Decision(str, Enum):
    """Admission decision returned by the concurrency gate."""

    ADMIT = "admit"
    WAIT = "wait"


#: Status codes / provider signals that mean "you are being rate limited".
_THROTTLE_STATUS = frozenset({429})
_THROTTLE_SIGNALS = frozenset({"throttled", "rate_limit", "rate_limited", "ratelimit"})


def is_throttled(status: int | None = None, signal: str | None = None) -> bool:
    """True when a response is a rate-limit/throttle, by HTTP status or provider signal string.

    Defensive: ``None``/garbage inputs simply return ``False``.
    """
    if status in _THROTTLE_STATUS:
        return True
    if signal is not None and str(signal).strip().lower() in _THROTTLE_SIGNALS:
        return True
    return False


def counts_toward_metrics(outcome: Outcome) -> bool:
    """Whether an outcome may contribute to latency/quality aggregates.

    Only :attr:`Outcome.COMPLETED` counts. Throttled and excluded calls are replay-pressure
    artifacts and must be kept out of the numbers; ``ADMITTED`` is still in flight (no result yet).
    """
    return outcome is Outcome.COMPLETED


def backoff_delay(
    attempt: int,
    *,
    base_s: float = DEFAULT_BASE_DELAY_S,
    max_s: float = DEFAULT_MAX_DELAY_S,
    rand: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with full jitter for retry scheduling.

    ``delay = random_in[0, min(max_s, base_s * 2**attempt)]`` (AWS "full jitter").

    ``attempt`` is 0-based (first retry = 0). Negative attempts are clamped to 0. ``rand`` is an
    injected ``random.random``-like callable returning ``[0, 1)`` — pass a seeded one for
    deterministic tests; defaults to :func:`random.random`. Never sleeps; just returns the seconds
    a caller *should* sleep.
    """
    attempt = max(0, int(attempt))
    base_s = max(0.0, float(base_s))
    max_s = max(0.0, float(max_s))
    # base_s * 2**attempt can overflow for huge attempts; cap the exponent cheaply.
    capped = min(max_s, base_s * (2.0 ** min(attempt, 32)))
    if rand is None:
        import random

        rand = random.random
    factor = rand()
    # Defensive: keep factor in [0, 1) even if an odd callable is injected.
    if not (0.0 <= factor < 1.0):
        factor = 0.0
    return capped * factor


@dataclass(frozen=True, slots=True)
class GovernorConfig:
    """Per-provider concurrency caps + backoff parameters.

    Unknown providers fall back to :attr:`default_max_concurrency` so a typo or a new provider
    degrades gracefully rather than crashing the replay.
    """

    max_concurrency: dict[str, int] = field(default_factory=dict)
    default_max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    base_delay_s: float = DEFAULT_BASE_DELAY_S
    max_delay_s: float = DEFAULT_MAX_DELAY_S

    def cap_for(self, provider: str) -> int:
        """Effective concurrency cap for ``provider`` (>= 1)."""
        cap = self.max_concurrency.get(provider, self.default_max_concurrency)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = self.default_max_concurrency
        return max(1, cap)


@dataclass(frozen=True, slots=True)
class ProviderCounters:
    """Immutable per-provider tally for a diagnostics snapshot."""

    provider: str
    in_flight: int
    admitted: int
    completed: int
    throttled: int
    excluded: int
    retried: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "provider": self.provider,
            "in_flight": self.in_flight,
            "admitted": self.admitted,
            "completed": self.completed,
            "throttled": self.throttled,
            "excluded": self.excluded,
            "retried": self.retried,
        }


@dataclass(frozen=True, slots=True)
class GovernorDiagnostics:
    """Immutable snapshot of governor state for replay diagnostics ("N throttled / excluded")."""

    providers: tuple[ProviderCounters, ...]
    total_admitted: int
    total_completed: int
    total_throttled: int
    total_excluded: int
    total_retried: int
    total_in_flight: int

    def as_dict(self) -> dict[str, object]:
        return {
            "providers": [p.as_dict() for p in self.providers],
            "total_admitted": self.total_admitted,
            "total_completed": self.total_completed,
            "total_throttled": self.total_throttled,
            "total_excluded": self.total_excluded,
            "total_retried": self.total_retried,
            "total_in_flight": self.total_in_flight,
        }


@dataclass(slots=True)
class _State:
    """Mutable per-provider counters. Counters are clamped to never go below zero."""

    in_flight: int = 0
    admitted: int = 0
    completed: int = 0
    throttled: int = 0
    excluded: int = 0
    retried: int = 0


@dataclass(frozen=True, slots=True)
class Admission:
    """Result of an :meth:`RateLimitGovernor.admit` call."""

    provider: str
    decision: Decision
    in_flight: int  # in-flight count *after* this decision was applied
    cap: int

    @property
    def admitted(self) -> bool:
        return self.decision is Decision.ADMIT


class RateLimitGovernor:
    """Synchronous, in-memory per-provider concurrency + throttle governor.

    The replay engine calls :meth:`admit` before issuing a call, :meth:`release` when it returns,
    :meth:`record_completed` / :meth:`record_throttle` to classify the result, and
    :meth:`backoff_delay` to schedule a retry. :meth:`diagnostics` produces the JSON-able snapshot.

    Thread-safe via a single lock around counter mutation; decision math is pure.
    """

    def __init__(self, config: GovernorConfig | None = None) -> None:
        self.config = config or GovernorConfig()
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    def _state(self, provider: str) -> _State:
        st = self._states.get(provider)
        if st is None:
            st = _State()
            self._states[provider] = st
        return st

    def cap_for(self, provider: str) -> int:
        return self.config.cap_for(provider)

    def in_flight(self, provider: str) -> int:
        with self._lock:
            st = self._states.get(provider)
            return st.in_flight if st else 0

    def would_admit(self, provider: str) -> bool:
        """Pure check: would a new call be admitted right now? (no state change)."""
        return self.in_flight(provider) < self.cap_for(provider)

    def admit(self, provider: str) -> Admission:
        """Decide admit vs. wait for ``provider`` and, if admitted, reserve an in-flight slot."""
        cap = self.cap_for(provider)
        with self._lock:
            st = self._state(provider)
            if st.in_flight < cap:
                st.in_flight += 1
                st.admitted += 1
                return Admission(provider, Decision.ADMIT, st.in_flight, cap)
            return Admission(provider, Decision.WAIT, st.in_flight, cap)

    def release(self, provider: str) -> None:
        """Free an in-flight slot (call exactly once per ADMIT, regardless of outcome)."""
        with self._lock:
            st = self._state(provider)
            st.in_flight = max(0, st.in_flight - 1)

    def record_completed(self, provider: str) -> None:
        """Mark a clean completion (counts toward metrics)."""
        with self._lock:
            self._state(provider).completed += 1

    def record_throttle(self, provider: str) -> None:
        """Mark a 429 / rate-limit. Excluded from metrics; surfaced in diagnostics."""
        with self._lock:
            self._state(provider).throttled += 1

    def record_excluded(self, provider: str) -> None:
        """Mark a call explicitly excluded from metrics for a non-quality reason."""
        with self._lock:
            self._state(provider).excluded += 1

    def record_retry(self, provider: str) -> None:
        """Mark that a retry was scheduled (for diagnostics / ban-avoidance auditing)."""
        with self._lock:
            self._state(provider).retried += 1

    def classify(self, status: int | None = None, signal: str | None = None) -> Outcome:
        """Map a raw response (status/signal) to an :class:`Outcome` without mutating state."""
        if is_throttled(status, signal):
            return Outcome.THROTTLED
        return Outcome.COMPLETED

    def backoff_delay(self, attempt: int, *, rand: Callable[[], float] | None = None) -> float:
        """Backoff for ``attempt`` using this governor's configured base/max delays."""
        return backoff_delay(
            attempt,
            base_s=self.config.base_delay_s,
            max_s=self.config.max_delay_s,
            rand=rand,
        )

    def diagnostics(self) -> GovernorDiagnostics:
        """Immutable snapshot of all per-provider counters + totals."""
        with self._lock:
            providers = tuple(
                ProviderCounters(
                    provider=name,
                    in_flight=st.in_flight,
                    admitted=st.admitted,
                    completed=st.completed,
                    throttled=st.throttled,
                    excluded=st.excluded,
                    retried=st.retried,
                )
                for name, st in sorted(self._states.items())
            )
        return GovernorDiagnostics(
            providers=providers,
            total_admitted=sum(p.admitted for p in providers),
            total_completed=sum(p.completed for p in providers),
            total_throttled=sum(p.throttled for p in providers),
            total_excluded=sum(p.excluded for p in providers),
            total_retried=sum(p.retried for p in providers),
            total_in_flight=sum(p.in_flight for p in providers),
        )
