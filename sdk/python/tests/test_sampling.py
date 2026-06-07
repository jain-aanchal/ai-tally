# SPDX-License-Identifier: Apache-2.0
from tally.sampling import (
    BillingMeter,
    Sampler,
    SamplingConfig,
    Stratum,
    TraceSignals,
    assign_stratum,
    extrapolate_cost,
)


def test_stratum_assignment():
    cfg = SamplingConfig()
    assert assign_stratum(TraceSignals(is_agent=True), cfg) is Stratum.TAIL
    assert assign_stratum(TraceSignals(model_tier="premium"), cfg) is Stratum.TAIL
    assert assign_stratum(TraceSignals(prompt_tokens_estimate=9000), cfg) is Stratum.TAIL
    assert assign_stratum(TraceSignals(prompt_tokens_estimate=3000), cfg) is Stratum.MID
    assert assign_stratum(TraceSignals(prompt_tokens_estimate=10), cfg) is Stratum.BODY


def test_decision_is_deterministic():
    s = Sampler(SamplingConfig(body_rate=0.5))
    d1 = s.decide("trace-xyz", TraceSignals())
    d2 = s.decide("trace-xyz", TraceSignals())
    assert d1 == d2


def test_tail_always_kept():
    s = Sampler(SamplingConfig(tail_rate=1.0))
    for i in range(200):
        d = s.decide(f"t{i}", TraceSignals(is_agent=True))
        assert d.keep is True
        assert d.stratum is Stratum.TAIL
        assert d.sample_rate == 1.0


def test_body_sampled_down_statistically():
    s = Sampler(SamplingConfig(body_rate=0.1))
    kept = sum(
        1 for i in range(10_000) if s.decide(f"id-{i}", TraceSignals()).keep
    )
    # ~10% with tolerance
    assert 800 <= kept <= 1200


def test_whole_trace_one_decision():
    # The decision depends only on trace_id, so every span in a trace agrees.
    s = Sampler(SamplingConfig(body_rate=0.3))
    d = s.decide("same-trace", TraceSignals())
    for _ in range(50):
        assert s.decide("same-trace", TraceSignals()).keep == d.keep


def test_feature_override():
    s = Sampler(SamplingConfig(body_rate=0.0, feature_overrides={"vip": 1.0}))
    assert s.decide("anything", TraceSignals(), feature_tag="vip").keep is True
    assert s.decide("anything", TraceSignals(), feature_tag="other").keep is False


def test_billing_counts_regardless_of_sampling():
    s = Sampler(SamplingConfig(body_rate=0.0))  # drop everything from analytics
    meter = BillingMeter()
    for i in range(100):
        tid = f"trace-{i}"
        meter.count_trace(tid)  # at HEAD, before sampling
        s.decide(tid, TraceSignals())  # would drop
    assert meter.trace_count == 100  # billing unaffected by sampling


def test_billing_dedup_per_trace():
    meter = BillingMeter()
    meter.count_trace("t1")
    meter.count_trace("t1")
    meter.count_trace("t2")
    assert meter.trace_count == 2


def test_extrapolation_tail_exact_body_scaled():
    # tail kept at 1.0 contributes exactly; body kept at 0.1 scales up ~10x
    samples = [(1000, 1.0), (50, 0.1)]
    assert extrapolate_cost(samples) == 1000 + 500
