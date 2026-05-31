"""Pure tests for the per-tenant rate limiter + monthly quota (CTO-33). No infra, no sleeps."""

from __future__ import annotations

import calendar

from gateway.errors import ErrorCode
from gateway.ratelimit import MonthlyQuota, RateLimiter, TokenBucket


class FakeClock:
    """A controllable wall clock in seconds."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


# --- TokenBucket ---------------------------------------------------------------------------------


def test_bucket_starts_full_and_drains() -> None:
    clk = FakeClock()
    b = TokenBucket(capacity=3, rps=1, clock=clk)
    assert b.try_consume() is True
    assert b.try_consume() is True
    assert b.try_consume() is True
    assert b.try_consume() is False  # empty


def test_bucket_refills_at_rps() -> None:
    clk = FakeClock()
    b = TokenBucket(capacity=2, rps=2, clock=clk)
    assert b.try_consume(2) is True
    assert b.try_consume() is False
    clk.advance(0.5)  # 0.5s * 2 rps = 1 token
    assert b.try_consume() is True
    assert b.try_consume() is False


def test_bucket_refill_capped_at_capacity() -> None:
    clk = FakeClock()
    b = TokenBucket(capacity=5, rps=100, clock=clk)
    b.try_consume(5)
    clk.advance(10)  # would add 1000 tokens, capped at 5
    assert b.try_consume(5) is True
    assert b.try_consume() is False


def test_bucket_retry_after_reflects_deficit() -> None:
    clk = FakeClock()
    b = TokenBucket(capacity=1, rps=2, clock=clk)
    assert b.try_consume() is True
    # need 1 token, have 0, at 2 rps → 0.5s
    assert abs(b.retry_after_s(1) - 0.5) < 1e-9


# --- MonthlyQuota --------------------------------------------------------------------------------


def test_quota_counts_and_blocks() -> None:
    clk = FakeClock(calendar.timegm((2026, 5, 10, 0, 0, 0, 0, 0, 0)))
    q = MonthlyQuota(limit=10, clock=clk)
    assert q.would_exceed("t", 10) is False
    q.consume("t", 8)
    assert q.remaining("t") == 2
    assert q.would_exceed("t", 3) is True
    assert q.would_exceed("t", 2) is False


def test_quota_resets_next_month() -> None:
    clk = FakeClock(calendar.timegm((2026, 5, 20, 0, 0, 0, 0, 0, 0)))
    q = MonthlyQuota(limit=5, clock=clk)
    q.consume("t", 5)
    assert q.remaining("t") == 0
    clk.t = calendar.timegm((2026, 6, 1, 0, 0, 0, 0, 0, 0))  # roll to June
    assert q.remaining("t") == 5


def test_quota_retry_after_points_at_next_month() -> None:
    start = calendar.timegm((2026, 5, 31, 23, 0, 0, 0, 0, 0))
    clk = FakeClock(start)
    q = MonthlyQuota(limit=1, clock=clk)
    # 1 hour until June 1 00:00 UTC
    assert abs(q.retry_after_s() - 3600) < 1.0


def test_quota_tenants_are_independent() -> None:
    clk = FakeClock()
    q = MonthlyQuota(limit=3, clock=clk)
    q.consume("a", 3)
    assert q.would_exceed("a", 1) is True
    assert q.would_exceed("b", 3) is False


# --- RateLimiter (combined) ----------------------------------------------------------------------


def test_limiter_allows_within_limits() -> None:
    clk = FakeClock()
    rl = RateLimiter(rps=10, burst=10, monthly_quota=1000, clock=clk)
    d = rl.check("t", 5)
    assert d.allowed is True
    assert rl.remaining_quota("t") == 995


def test_limiter_rate_limits_then_recovers() -> None:
    clk = FakeClock()
    rl = RateLimiter(rps=10, burst=10, monthly_quota=1000, clock=clk)
    assert rl.check("t", 10).allowed is True
    d = rl.check("t", 1)
    assert d.allowed is False
    assert d.code == ErrorCode.RATE_LIMITED
    assert d.retry_after_ms > 0
    clk.advance(1.0)  # +10 tokens
    assert rl.check("t", 1).allowed is True


def test_limiter_quota_exceeded_does_not_burn_rate_tokens() -> None:
    clk = FakeClock()
    rl = RateLimiter(rps=100, burst=100, monthly_quota=5, clock=clk)
    assert rl.check("t", 5).allowed is True
    d = rl.check("t", 1)
    assert d.allowed is False
    assert d.code == ErrorCode.QUOTA_EXCEEDED
    assert d.retry_after_s > 0


def test_limiter_rejected_batch_consumes_no_quota() -> None:
    clk = FakeClock()
    # burst smaller than the request → rate-limited before quota is touched
    rl = RateLimiter(rps=1, burst=2, monthly_quota=1000, clock=clk)
    d = rl.check("t", 5)
    assert d.allowed is False
    assert d.code == ErrorCode.RATE_LIMITED
    assert rl.remaining_quota("t") == 1000  # untouched


def test_limiter_tenants_isolated() -> None:
    clk = FakeClock()
    rl = RateLimiter(rps=1, burst=2, monthly_quota=1000, clock=clk)
    assert rl.check("a", 2).allowed is True
    assert rl.check("a", 1).allowed is False  # a is drained
    assert rl.check("b", 2).allowed is True  # b unaffected
