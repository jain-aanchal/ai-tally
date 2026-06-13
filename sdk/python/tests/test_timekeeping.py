# SPDX-License-Identifier: Apache-2.0
from tally.timekeeping import (
    NS_PER_SECOND,
    assess,
    effective_timestamp_ns,
    is_skewed,
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
