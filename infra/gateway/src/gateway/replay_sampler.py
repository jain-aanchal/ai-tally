"""Stratified sampler + PII scrubber for replay capture (CTO-113).

Sampling is per-tenant opt-in and stratified by ``(feature_tag, token-count quintile)`` so that
*small-but-expensive* runs — short prompt, huge response, exotic model — don't get drowned out by
the long tail of cheap chat turns. The sample target is ``sample_rate`` of the batch overall, but
within that we over-weight high-token strata so the replay set covers cost outliers.

PII scrubbing runs **before** the resolved-context payload is written to object storage. Three
classes of redaction:

* **emails** — the same regex `gateway.validation` rejects at ingest (defense in depth)
* **API keys** — common prefixes (sk-, sk_live_, sk_test_, whsec_, rk_, pk_, xoxb-, ghp_, ...)
* **postal addresses** — a deliberately conservative regex (street-number + street + city); we
  prefer false negatives over false positives, but the obvious cases get caught.

Output of the sampler is :class:`ReplaySamplePayload` records — the gateway hands those to its
object_store wrapper and then writes an index row to ClickHouse.
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID, uuid4

# Reuse the validator's email regex so scrubbing and ingest stay in lockstep.
from gateway.validation import _EMAIL_RE

# Common provider API-key prefixes. Match the prefix then a run of url-safe key chars; tuned to
# *catch* secrets like `sk-test_abc123_xyz` without eating arbitrary tokens that happen to share
# a prefix. Keep this list narrow and additive — false positives mean less useful replay corpora.
_API_KEY_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_\-]{12,}"
    r"|sk_live_[A-Za-z0-9]{12,}"
    r"|sk_test_[A-Za-z0-9]{12,}"
    r"|rk_live_[A-Za-z0-9]{12,}"
    r"|pk_live_[A-Za-z0-9]{12,}"
    r"|whsec_[A-Za-z0-9]{12,}"
    r"|xoxb-[A-Za-z0-9\-]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r")\b"
)

# Postal-address heuristic: digits + street name + suffix (St/Ave/Rd/Blvd/Ln/Dr/Way/Ct/Pl).
# Deliberately conservative — we'd rather miss a weird format than redact every number.
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl)\b",
    re.IGNORECASE,
)

REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_KEY = "[REDACTED_KEY]"
REDACTED_ADDRESS = "[REDACTED_ADDRESS]"


def scrub_pii(text: str) -> str:
    """Replace emails, API keys, and postal addresses in ``text`` with redaction sentinels.

    Order matters: scrub keys first (their alnum runs can otherwise collide with the address
    heuristic), then emails, then addresses.
    """
    if not text:
        return text
    out = _API_KEY_RE.sub(REDACTED_KEY, text)
    out = _EMAIL_RE.sub(REDACTED_EMAIL, out)
    out = _ADDRESS_RE.sub(REDACTED_ADDRESS, out)
    return out


def scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub a JSON-like payload — strings get :func:`scrub_pii`, structure is preserved."""
    if isinstance(payload, str):
        return scrub_pii(payload)
    if isinstance(payload, dict):
        return {k: scrub_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [scrub_payload(v) for v in payload]
    return payload


@dataclass(frozen=True, slots=True)
class SampleCandidate:
    """One ingested span considered for sampling. The gateway maps each span to this shape."""

    trace_id: str
    span_id: str
    feature_tag: str
    real_provider: str
    real_model: str
    input_tokens: int
    output_tokens: int
    # The full resolved request envelope: prompt, tools, model config, response. Free-form JSON.
    envelope: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReplaySamplePayload:
    """Output of the sampler: scrubbed, ready-to-store sample + index row fields."""

    sample_id: UUID
    trace_id: str
    feature_tag: str
    real_provider: str
    real_model: str
    input_tokens: int
    output_tokens: int
    # The PII-scrubbed JSON envelope, serialized — what the executor will replay.
    scrubbed_json: bytes
    pii_scrubbed: bool = True


def _token_quintile(total_tokens: int, ceilings: Sequence[int]) -> int:
    """0..4 — which quintile of the batch's token-count distribution this span sits in."""
    for i, ceiling in enumerate(ceilings):
        if total_tokens <= ceiling:
            return i
    return len(ceilings)


def _compute_quintile_ceilings(token_totals: list[int]) -> list[int]:
    """Quintile cuts at 20/40/60/80% of the sorted token totals (inclusive upper bound per cut)."""
    if not token_totals:
        return [0, 0, 0, 0]
    s = sorted(token_totals)
    n = len(s)
    return [s[min(n - 1, math.ceil(n * q / 5) - 1)] for q in (1, 2, 3, 4)]


def stratified_sample(
    candidates: list[SampleCandidate],
    *,
    sample_rate: float,
    rng: random.Random | None = None,
) -> list[SampleCandidate]:
    """Pick a stratified subset of ``candidates`` at roughly ``sample_rate``.

    Strata are ``(feature_tag, token-quintile)``. Within a stratum we sample uniformly. Strata
    in the **top token quintile** are sampled at 2x the base rate (capped at 1.0), so the replay
    corpus over-represents costly tail traffic — exactly the runs where a cheaper candidate model
    pays off the most.

    Deterministic given ``rng``. For prod ingest the caller passes ``random.Random(span_id)`` or
    similar so re-runs of the same batch don't double-sample.
    """
    if not candidates or sample_rate <= 0:
        return []
    rng = rng or random.Random()
    sample_rate = max(0.0, min(1.0, sample_rate))

    token_totals = [c.input_tokens + c.output_tokens for c in candidates]
    ceilings = _compute_quintile_ceilings(token_totals)

    # Bucket by (feature_tag, quintile). Each bucket samples independently — uniform within.
    buckets: dict[tuple[str, int], list[SampleCandidate]] = {}
    for c, total in zip(candidates, token_totals, strict=True):
        q = _token_quintile(total, ceilings)
        buckets.setdefault((c.feature_tag, q), []).append(c)

    picked: list[SampleCandidate] = []
    for (_tag, quintile), bucket in buckets.items():
        # Over-weight the top quintile so high-token runs are well-represented in the corpus.
        effective_rate = sample_rate * (2.0 if quintile >= 3 else 1.0)
        effective_rate = min(1.0, effective_rate)
        target = max(0, int(round(len(bucket) * effective_rate)))
        if target == 0 and effective_rate > 0 and rng.random() < effective_rate * len(bucket):
            # Tiny buckets — preserve a chance to sample at all so a singleton outlier stratum
            # isn't always dropped.
            target = 1
        if target >= len(bucket):
            picked.extend(bucket)
        else:
            picked.extend(rng.sample(bucket, target))

    return picked


def build_payloads(
    sampled: list[SampleCandidate],
    *,
    scrub: bool = True,
) -> list[ReplaySamplePayload]:
    """Scrub + serialize each sampled candidate for storage."""
    out: list[ReplaySamplePayload] = []
    for c in sampled:
        envelope = scrub_payload(c.envelope) if scrub else c.envelope
        out.append(
            ReplaySamplePayload(
                sample_id=uuid4(),
                trace_id=c.trace_id,
                feature_tag=c.feature_tag,
                real_provider=c.real_provider,
                real_model=c.real_model,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                scrubbed_json=json.dumps(envelope, sort_keys=True, default=str).encode("utf-8"),
                pii_scrubbed=scrub,
            )
        )
    return out
