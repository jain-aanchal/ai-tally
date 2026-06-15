"""Unit tests for the replay sampler + PII scrubber (CTO-113)."""

from __future__ import annotations

import json
import random

from gateway.replay_sampler import (
    REDACTED_ADDRESS,
    REDACTED_EMAIL,
    REDACTED_KEY,
    SampleCandidate,
    build_payloads,
    scrub_payload,
    scrub_pii,
    stratified_sample,
)


def _candidate(
    tag: str = "chat",
    input_tokens: int = 100,
    output_tokens: int = 50,
    *,
    trace: str = "trace",
    envelope: dict | None = None,
) -> SampleCandidate:
    return SampleCandidate(
        trace_id=trace,
        span_id=trace + "-s",
        feature_tag=tag,
        real_provider="anthropic",
        real_model="claude-sonnet-4.5",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        envelope=envelope or {"prompt": "hello"},
    )


# --- PII scrubber ----------------------------------------------------------------

def test_scrub_pii_redacts_emails_and_api_keys() -> None:
    raw = "send a message to alice@example.com using sk-test_abcdef1234567890"
    scrubbed = scrub_pii(raw)
    assert "alice@example.com" not in scrubbed
    assert "sk-test_abcdef1234567890" not in scrubbed
    assert REDACTED_EMAIL in scrubbed
    assert REDACTED_KEY in scrubbed


def test_scrub_pii_redacts_address() -> None:
    raw = "ship to 1234 Main Street, Springfield."
    scrubbed = scrub_pii(raw)
    assert "1234 Main Street" not in scrubbed
    assert REDACTED_ADDRESS in scrubbed


def test_scrub_payload_walks_nested_structure() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "hi bob@example.com"},
            {"role": "assistant", "content": "ok"},
        ],
        "meta": {"customer_email": "alice@a.io"},
    }
    out = scrub_payload(payload)
    flat = json.dumps(out)
    assert "bob@example.com" not in flat
    assert "alice@a.io" not in flat
    assert REDACTED_EMAIL in flat


def test_build_payloads_writes_scrubbed_envelope() -> None:
    cand = _candidate(envelope={"prompt": "email alice@example.com sk-test_abcdef1234567890"})
    payloads = build_payloads([cand], scrub=True)
    assert len(payloads) == 1
    body = payloads[0].scrubbed_json.decode("utf-8")
    assert "alice@example.com" not in body
    assert "sk-test_abcdef1234567890" not in body
    assert REDACTED_EMAIL in body
    assert REDACTED_KEY in body
    assert payloads[0].pii_scrubbed is True


# --- Stratified sampler ---------------------------------------------------------

def test_stratified_sample_overweights_high_token_runs() -> None:
    # 95 cheap and 5 expensive candidates. At a base sample rate of 5%, naive uniform sampling
    # would catch ~5 cheap + 0.25 expensive — the expensive stratum would mostly miss. The
    # sampler over-weights the top quintile, so we expect to see the expensive runs picked
    # disproportionately.
    cheap = [_candidate(input_tokens=10, output_tokens=10, trace=f"c{i}") for i in range(95)]
    expensive = [_candidate(input_tokens=2_000, output_tokens=2_000, trace=f"x{i}") for i in range(5)]
    rng = random.Random(42)
    sampled = stratified_sample(cheap + expensive, sample_rate=0.05, rng=rng)
    expensive_picks = [s for s in sampled if s.input_tokens >= 2_000]
    # Expensive stratum is 5% of the corpus but should be massively over-represented in the
    # sample versus the base rate. With 5 candidates and 2x weighting + min-1 floor we expect
    # at least 1 expensive pick.
    assert len(expensive_picks) >= 1, sampled
    # Sanity: total sampled stays roughly near the requested rate (with a small over-tilt).
    assert 1 <= len(sampled) <= 20


def test_stratified_sample_deterministic_with_seeded_rng() -> None:
    cands = [_candidate(input_tokens=i * 10, trace=f"t{i}") for i in range(50)]
    s1 = stratified_sample(cands, sample_rate=0.2, rng=random.Random(7))
    s2 = stratified_sample(cands, sample_rate=0.2, rng=random.Random(7))
    assert [c.trace_id for c in s1] == [c.trace_id for c in s2]


def test_stratified_sample_empty_inputs() -> None:
    assert stratified_sample([], sample_rate=0.5) == []
    cands = [_candidate(trace=f"t{i}") for i in range(10)]
    assert stratified_sample(cands, sample_rate=0.0) == []
