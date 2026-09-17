# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone

from tally.timekeeping import (
    MAX_REPRESENTABLE_TS_NS,
    MIN_REPRESENTABLE_TS_NS,
    NS_PER_SECOND,
    assess,
    effective_timestamp_ns,
    is_skewed,
    representable_ts_ns,
    skew_seconds,
)


def _s(seconds: float) -> int:
    return int(seconds * NS_PER_SECOND)


def test_client_in_past_used_as_is():
    client = _s(1000)
    server = _s(1010)  # server later → client is in the past
    assert effective_timestamp_ns(client, server) == client


def test_client_slightly_ahead_within_tolerance_used_as_is():
    server = _s(1000)
    client = _s(1000 + 600)  # 10 min ahead, under 1h ceiling
    assert effective_timestamp_ns(client, server) == client


def test_runaway_future_client_clamped():
    server = _s(1000)
    client = _s(1000 + 10 * 3600)  # 10h ahead
    eff = effective_timestamp_ns(client, server)
    assert eff == server + 3600 * NS_PER_SECOND  # clamped to server + 1h
    assert eff < client


def test_skew_seconds_signed():
    assert skew_seconds(_s(1100), _s(1000)) == 100.0   # client ahead
    assert skew_seconds(_s(900), _s(1000)) == -100.0   # client behind


def test_is_skewed_threshold():
    assert is_skewed(_s(1000 + 400), _s(1000)) is True   # 400s > 300s
    assert is_skewed(_s(1000 + 100), _s(1000)) is False  # within 300s
    assert is_skewed(_s(1000 - 400), _s(1000)) is True   # behind also counts


def test_assess_clamped_and_flagged():
    server = _s(1000)
    client = _s(1000 + 10 * 3600)
    a = assess(client, server)
    assert a.clamped is True
    assert a.skewed is True
    assert a.skew_s == 10 * 3600
    assert a.effective_ts_ns == server + 3600 * NS_PER_SECOND


def test_assess_clean_case():
    server = _s(1000)
    client = _s(1001)
    a = assess(client, server)
    assert a.clamped is False
    assert a.skewed is False
    assert a.effective_ts_ns == client


def test_a_normal_timestamp_passes_through_representable_untouched():
    """The clamp is a guard, not a transform: real traffic must be unaffected by it."""
    ts = _s(1_700_000_000)
    assert representable_ts_ns(ts) == ts


def test_an_out_of_range_timestamp_is_clamped_rather_than_raising():
    """CTO-416 review: this is what stops one absurd clock from dropping a whole batch.

    effective_timestamp_ns bounds the future side only, so the past side has to be bounded here or
    datetime.fromtimestamp raises on the ingest path and every span in the batch is lost with it.
    """
    for ts_ns in (-(10**23), 10**23):
        # The point is that the conversion the ingest path performs no longer raises.
        datetime.fromtimestamp(representable_ts_ns(ts_ns) / 1e9, tz=timezone.utc)

    assert representable_ts_ns(-(10**23)) == MIN_REPRESENTABLE_TS_NS
    assert representable_ts_ns(10**23) == MAX_REPRESENTABLE_TS_NS
    assert datetime.fromtimestamp(MIN_REPRESENTABLE_TS_NS / 1e9, tz=timezone.utc).year == 1
    assert datetime.fromtimestamp(MAX_REPRESENTABLE_TS_NS / 1e9, tz=timezone.utc).year == 9999
