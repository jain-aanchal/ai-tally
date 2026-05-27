"""Clock-skew handling for ingest timestamps.

Implements CTO-38. Spec §12.9.

Spans carry a client-emitted timestamp; the gateway also records when it received the batch.
Client clocks drift, and a future-dated client timestamp would poison time-bucketed rollups. So:

- the **effective** timestamp used for rollups is ``min(client_ts, server_recv_ts + max_future)``
  — a slightly-fast client is tolerated up to ``max_future`` (default 1h), a wildly-future client
  is clamped to "now-ish";
- skew beyond a threshold (default 5 min) is flagged per tenant for monitoring.

Pure functions over nanosecond epoch timestamps — no infra.
"""

from __future__ import annotations

from dataclasses import dataclass

NS_PER_SECOND = 1_000_000_000
DEFAULT_MAX_FUTURE_S = 3600  # 1 hour
DEFAULT_SKEW_THRESHOLD_S = 300  # 5 minutes


def effective_timestamp_ns(
    client_ts_ns: int,
    server_recv_ts_ns: int,
    *,
    max_future_s: int = DEFAULT_MAX_FUTURE_S,
) -> int:
    """Timestamp to use for storage/rollups: ``min(client_ts, server_recv + max_future)``.

    A client behind the server clock is used as-is (past timestamps are fine). A client ahead of
    the server by more than ``max_future`` is clamped so it can't land in a future rollup bucket.
    """
    ceiling = server_recv_ts_ns + max_future_s * NS_PER_SECOND
    return min(client_ts_ns, ceiling)


def skew_seconds(client_ts_ns: int, server_recv_ts_ns: int) -> float:
    """Signed skew in seconds: positive = client ahead of server."""
    return (client_ts_ns - server_recv_ts_ns) / NS_PER_SECOND


def is_skewed(
    client_ts_ns: int,
    server_recv_ts_ns: int,
    *,
    threshold_s: int = DEFAULT_SKEW_THRESHOLD_S,
) -> bool:
    """True when |skew| exceeds the threshold (default 5 min)."""
    return abs(skew_seconds(client_ts_ns, server_recv_ts_ns)) > threshold_s


@dataclass(frozen=True, slots=True)
class SkewAssessment:
    effective_ts_ns: int
    skew_s: float
    skewed: bool
    clamped: bool


def assess(
    client_ts_ns: int,
    server_recv_ts_ns: int,
    *,
    max_future_s: int = DEFAULT_MAX_FUTURE_S,
    threshold_s: int = DEFAULT_SKEW_THRESHOLD_S,
) -> SkewAssessment:
    """One-shot: effective timestamp + skew + flags. The gateway logs ``skewed`` per tenant."""
    eff = effective_timestamp_ns(client_ts_ns, server_recv_ts_ns, max_future_s=max_future_s)
    return SkewAssessment(
        effective_ts_ns=eff,
        skew_s=skew_seconds(client_ts_ns, server_recv_ts_ns),
        skewed=is_skewed(client_ts_ns, server_recv_ts_ns, threshold_s=threshold_s),
        clamped=eff < client_ts_ns,
    )
