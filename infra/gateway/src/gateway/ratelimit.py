"""Per-tenant rate limiting + monthly quota — pure, in-memory, clock-injectable.

Two independent guards (CTO-33, spec §12.1/§4.5):

* **Rate limit** — a token bucket per tenant smooths short bursts. Capacity = burst, refilled at
  ``rps`` tokens/sec. One token per span. Empty bucket → ``RATE_LIMITED`` with a ``retry_after``.
* **Quota** — a monthly span ceiling per tenant. Spent quota → ``QUOTA_EXCEEDED`` with a
  ``retry_after`` pointing at the start of next month.

Both are process-local: this is the single-node enforcement layer. Cluster-wide fairness (shared
Redis counters) is a later infra concern (CTO-30); the contract returned here — a
:class:`Decision` with a stable :class:`~gateway.errors.ErrorCode` and a ``retry_after`` — does not
change when that lands.

The clock is injectable so the whole module tests deterministically with zero sleeps.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from gateway.errors import ErrorCode

# A monotonic-ish wall clock in seconds. Injectable for tests.
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of a limiter check. ``allowed`` true → proceed; else ``code``/``retry_after_s`` set."""

    allowed: bool
    code: ErrorCode | None = None
    retry_after_s: float = 0.0
    message: str = ""

    @property
    def retry_after_ms(self) -> int:
        return int(self.retry_after_s * 1000)


_ALLOW = Decision(allowed=True)


class TokenBucket:
    """Classic token bucket. ``capacity`` tokens, refilled at ``rps``/sec, lazily on read."""

    __slots__ = ("capacity", "rps", "_tokens", "_last", "_clock")

    def __init__(self, capacity: float, rps: float, clock: Clock, *, start_full: bool = True) -> None:
        if capacity <= 0 or rps <= 0:
            raise ValueError("capacity and rps must be positive")
        self.capacity = float(capacity)
        self.rps = float(rps)
        self._tokens = float(capacity) if start_full else 0.0
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)
            self._last = now

    def try_consume(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def retry_after_s(self, n: float = 1.0) -> float:
        """Seconds until ``n`` tokens are available (0 if already available)."""
        self._refill()
        deficit = n - self._tokens
        return max(0.0, deficit / self.rps)


class MonthlyQuota:
    """A per-tenant span counter that resets at the start of each UTC month.

    ``month_key`` buckets usage; when the key rolls over the counter resets. Cheap and exact enough
    for single-node enforcement; the authoritative billing meter is a separate ledger (CTO-84/85).
    """

    __slots__ = ("limit", "_clock", "_used", "_month")

    def __init__(self, limit: int, clock: Clock) -> None:
        self.limit = int(limit)
        self._clock = clock
        self._used: dict[str, int] = {}
        self._month: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _ym(epoch_s: float) -> tuple[int, int]:
        import time as _time

        t = _time.gmtime(epoch_s)
        return (t.tm_year, t.tm_mon)

    @staticmethod
    def _seconds_until_next_month(epoch_s: float) -> float:
        import calendar
        import time as _time

        t = _time.gmtime(epoch_s)
        year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
        next_start = calendar.timegm((year, month, 1, 0, 0, 0, 0, 0, 0))
        return max(0.0, next_start - epoch_s)

    def _roll(self, tenant: str) -> None:
        cur = self._ym(self._clock())
        if self._month.get(tenant) != cur:
            self._month[tenant] = cur
            self._used[tenant] = 0

    def remaining(self, tenant: str) -> int:
        self._roll(tenant)
        return max(0, self.limit - self._used.get(tenant, 0))

    def would_exceed(self, tenant: str, n: int) -> bool:
        self._roll(tenant)
        return self._used.get(tenant, 0) + n > self.limit

    def consume(self, tenant: str, n: int) -> None:
        self._roll(tenant)
        self._used[tenant] = self._used.get(tenant, 0) + n

    def retry_after_s(self) -> float:
        return self._seconds_until_next_month(self._clock())


class RateLimiter:
    """Combined per-tenant rate + quota guard. Thread-safe; clock-injectable.

    ``check(tenant, n)`` returns a :class:`Decision`. It is *side-effecting on success*: it consumes
    ``n`` rate tokens and ``n`` quota units only when both guards pass, so a rejected batch never
    burns budget. Rate is checked before quota (cheap, transient) so an over-quota tenant still gets
    the more-actionable QUOTA_EXCEEDED only once past the burst gate.
    """

    def __init__(
        self,
        *,
        rps: float,
        burst: float,
        monthly_quota: int,
        clock: Clock | None = None,
    ) -> None:
        import time as _time

        self._rps = rps
        self._burst = burst
        # One wall clock for both guards keeps tests deterministic (inject a fake) and keeps the
        # quota's month-boundary math correct (token buckets only care about elapsed deltas).
        self._clock: Clock = clock or _time.time
        self._buckets: dict[str, TokenBucket] = {}
        self._quota = MonthlyQuota(monthly_quota, self._clock)
        self._lock = threading.Lock()

    def _bucket(self, tenant: str) -> TokenBucket:
        b = self._buckets.get(tenant)
        if b is None:
            b = TokenBucket(self._burst, self._rps, self._clock)
            self._buckets[tenant] = b
        return b

    def check(self, tenant: str, n: int = 1) -> Decision:
        with self._lock:
            bucket = self._bucket(tenant)
            # Quota first as a read-only test so we don't consume tokens for a doomed batch.
            if self._quota.would_exceed(tenant, n):
                return Decision(
                    allowed=False,
                    code=ErrorCode.QUOTA_EXCEEDED,
                    retry_after_s=self._quota.retry_after_s(),
                    message="monthly span quota exhausted",
                )
            if not bucket.try_consume(n):
                return Decision(
                    allowed=False,
                    code=ErrorCode.RATE_LIMITED,
                    retry_after_s=bucket.retry_after_s(n),
                    message="per-tenant rate limit exceeded",
                )
            self._quota.consume(tenant, n)
            return _ALLOW

    def remaining_quota(self, tenant: str) -> int:
        with self._lock:
            return self._quota.remaining(tenant)
