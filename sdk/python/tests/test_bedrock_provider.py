# SPDX-License-Identifier: Apache-2.0
"""Amazon Bedrock as a first-class LLM cost provider (CTO-157).

Mirror of the CTO-149 Gemini coverage. Proves the three things the ticket asks the SDK side
to demonstrate:
  1. the seed catalog prices the Bedrock lineup under the ``bedrock/`` provider prefix
     (non-zero, correct against the seeded rates, no collision with vendor-direct entries);
  2. ``record_llm_call(provider="bedrock", ...)`` costs from the catalog and stamps
     ``gen_ai.system="bedrock"`` on a conformant span — no provider allowlist to trip over;
  3. model discovery lists foundation models via an INJECTED Bedrock control-plane client
     (never the network), classifies them into families, and fails soft when unavailable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tally import models as M
from tally.client import MemoryExporter, TallyClient
from tally.context import with_trace_context
from tally.pricing import (
    PriceType,
    Usage,
    compute_cost_micro_usd,
    seed_catalog,
)
from tally.sampling import Sampler, SamplingConfig
from tally.schema import GenAI, validate_span_attributes

AT = date(2026, 6, 1)


# --- Price catalog ------------------------------------------------------------


def test_bedrock_claude_sonnet_priced_correctly() -> None:
    cat = seed_catalog()
    # 1M input @ $3.30/MTok + 1M output @ $16.50/MTok = $19.80 = 19_800_000 micro-USD.
    cost, version = compute_cost_micro_usd(
        cat, "bedrock", "anthropic.claude-sonnet-4-5", Usage(1_000_000, 1_000_000), at=AT
    )
    assert cost == 19_800_000
    assert version  # a real catalog version, not the empty "partial price" sentinel


def test_bedrock_nova_micro_priced_correctly() -> None:
    cat = seed_catalog()
    # 1M input @ $0.035/MTok + 1M output @ $0.14/MTok = $0.175 = 175_000 micro-USD.
    cost, version = compute_cost_micro_usd(
        cat, "bedrock", "amazon.nova-micro", Usage(1_000_000, 1_000_000), at=AT
    )
    assert cost == 175_000
    assert version


def test_bedrock_reprices_above_vendor_direct() -> None:
    # The whole point of a separate provider dimension: Bedrock's managed rate is NOT the
    # Anthropic-direct rate. Sonnet output is $16.50 on Bedrock vs $15.00 vendor-direct.
    cat = seed_catalog()
    bedrock = cat.lookup("bedrock", "anthropic.claude-sonnet-4-5", PriceType.OUTPUT, at=AT)
    direct = cat.lookup("anthropic", "claude-sonnet-4-5", PriceType.OUTPUT, at=AT)
    assert bedrock is not None and direct is not None
    assert bedrock.price_per_unit > direct.price_per_unit


def test_bedrock_does_not_collide_with_vendor_direct() -> None:
    # The vendor-direct Anthropic key must be untouched by the Bedrock entries.
    cat = seed_catalog()
    # Bedrock uses the dotted native modelId; vendor-direct uses the bare slug.
    assert cat.lookup("bedrock", "claude-sonnet-4-5", PriceType.INPUT, at=AT) is None
    assert cat.lookup("anthropic", "anthropic.claude-sonnet-4-5", PriceType.INPUT, at=AT) is None
    direct = cat.lookup("anthropic", "claude-sonnet-4-5", PriceType.INPUT, at=AT)
    assert direct is not None and direct.price_per_unit == Decimal("3.00")


def test_bedrock_llama_has_no_cached_tier_but_still_prices() -> None:
    cat = seed_catalog()
    assert (
        cat.lookup("bedrock", "meta.llama3-3-70b-instruct", PriceType.CACHED_INPUT, at=AT) is None
    )
    cost, version = compute_cost_micro_usd(
        cat, "bedrock", "meta.llama3-3-70b-instruct", Usage(1000, 250), at=AT
    )
    assert cost > 0
    assert version


def test_bedrock_cached_tier_is_cheaper() -> None:
    cat = seed_catalog()
    cached = cat.lookup("bedrock", "amazon.nova-pro", PriceType.CACHED_INPUT, at=AT)
    standard = cat.lookup("bedrock", "amazon.nova-pro", PriceType.INPUT, at=AT)
    assert cached is not None and standard is not None
    assert cached.price_per_unit < standard.price_per_unit


def test_bedrock_rates_are_decimal() -> None:
    cat = seed_catalog()
    entry = cat.lookup("bedrock", "amazon.titan-text-express", PriceType.OUTPUT, at=AT)
    assert entry is not None
    assert isinstance(entry.price_per_unit, Decimal)


def test_bedrock_cost_is_nonzero_for_small_usage() -> None:
    cat = seed_catalog()
    for model in (
        "anthropic.claude-sonnet-4-5",
        "anthropic.claude-haiku-4-5",
        "meta.llama3-3-70b-instruct",
        "amazon.nova-pro",
        "amazon.nova-micro",
        "amazon.titan-text-express",
    ):
        cost, version = compute_cost_micro_usd(cat, "bedrock", model, Usage(1000, 250), at=AT)
        assert cost > 0, model
        assert version, model


# --- Provider path (record_llm_call) ------------------------------------------


def _client(**kw) -> TallyClient:
    return TallyClient(catalog=seed_catalog(), **kw)


def test_record_llm_call_bedrock_costs_from_catalog() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter, sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with with_trace_context(trace_id="t1", feature_tag="compare", session_id="s1"):
        result = client.record_llm_call(
            provider="bedrock",
            model="anthropic.claude-sonnet-4-5",
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
    assert result.kept is True
    assert result.cost_micro_usd == 19_800_000
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.SYSTEM] == "bedrock"
    assert span[GenAI.COST_ESTIMATED_MICRO_USD] == 19_800_000


def test_record_llm_call_bedrock_maps_cached_tokens() -> None:
    # cacheReadInputTokens -> cached_input_tokens; billed at the cheaper cached rate.
    client = _client(sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with with_trace_context(trace_id="t2"):
        full = client.record_llm_call(
            provider="bedrock",
            model="amazon.nova-pro",
            usage=Usage(input_tokens=1_000_000, output_tokens=0),
        )
        cached = client.record_llm_call(
            provider="bedrock",
            model="amazon.nova-pro",
            usage=Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
        )
    assert full.cost_micro_usd is not None and cached.cost_micro_usd is not None
    assert cached.cost_micro_usd < full.cost_micro_usd


# --- Model discovery (injected client — never the network) --------------------


class _FakeBedrock:
    """Stand-in for a boto3 ``bedrock`` control-plane client."""

    def __init__(self, summaries: list[dict]):
        self._summaries = summaries

    def list_foundation_models(self, **_kw) -> dict:
        return {"modelSummaries": self._summaries}


_SUMMARIES = [
    {"modelId": "anthropic.claude-sonnet-4-5-20250101-v1:0", "outputModalities": ["TEXT"]},
    {"modelId": "anthropic.claude-haiku-4-5-20250101-v1:0", "outputModalities": ["TEXT"]},
    {"modelId": "meta.llama3-3-70b-instruct-v1:0", "outputModalities": ["TEXT"]},
    {"modelId": "amazon.nova-pro-v1:0", "outputModalities": ["TEXT"]},
    {"modelId": "amazon.titan-text-express-v1", "outputModalities": ["TEXT"]},
    # Non-chat SKUs that must be filtered out by the modality guard:
    {"modelId": "amazon.titan-embed-text-v2:0", "outputModalities": ["EMBEDDING"]},
    {"modelId": "amazon.titan-image-generator-v1", "outputModalities": ["IMAGE"]},
]


def test_fetch_bedrock_models_parses_and_classifies() -> None:
    out = M.fetch_bedrock_models(client=_FakeBedrock(_SUMMARIES))
    ids = {m.id for m in out}
    # Text models kept, embedding/image dropped.
    assert "anthropic.claude-sonnet-4-5-20250101-v1:0" in ids
    assert "meta.llama3-3-70b-instruct-v1:0" in ids
    assert "amazon.titan-embed-text-v2:0" not in ids
    assert "amazon.titan-image-generator-v1" not in ids
    assert all(m.provider == "bedrock" for m in out)
    # Families are classified consistently with classify_family.
    by_id = {m.id: m for m in out}
    assert by_id["anthropic.claude-sonnet-4-5-20250101-v1:0"].family == "sonnet"
    assert by_id["anthropic.claude-haiku-4-5-20250101-v1:0"].family == "haiku"
    # Llama's "instruct" routes to "other" (CTO-172 non-chat-family guard keyword), Titan/Nova too.
    assert by_id["meta.llama3-3-70b-instruct-v1:0"].family == "other"
    assert by_id["amazon.titan-text-express-v1"].family == "other"
    # latest() can resolve a Bedrock family.
    sonnet = M.latest("bedrock", "sonnet", out)
    assert sonnet is not None and sonnet.family == "sonnet"


def test_classify_bedrock_families() -> None:
    assert M.classify_family("anthropic.claude-sonnet-4-5-20250101-v1:0") == "sonnet"
    assert M.classify_family("anthropic.claude-haiku-4-5") == "haiku"


def test_discover_includes_bedrock_with_injected_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No AWS env needed: injecting a client is itself the enable signal.
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(
        cache_path=tmp_path / "models.json",
        bedrock_client=_FakeBedrock(_SUMMARIES),
    )
    bedrock_ids = {m.id for m in result if m.provider == "bedrock"}
    assert "amazon.nova-pro-v1:0" in bedrock_ids
    # Non-text SKUs never made it into the discovered lineup.
    assert "amazon.titan-embed-text-v2:0" not in bedrock_ids


def test_discover_skips_bedrock_when_unconfigured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No injected client and no AWS env → Bedrock is skipped, no crash, empty result.
    for var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(cache_path=tmp_path / "absent.json")
    assert [m for m in result if m.provider == "bedrock"] == []


def test_discover_bedrock_fails_soft_on_client_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A control-plane error (e.g. AccessDenied / no creds) must skip Bedrock, not crash discovery.
    class _BoomBedrock:
        def list_foundation_models(self, **_kw):
            raise RuntimeError("AccessDeniedException")

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(
        cache_path=tmp_path / "absent.json",
        bedrock_client=_BoomBedrock(),
    )
    assert result == []


def test_aws_configured_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    assert M._aws_configured() is False
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert M._aws_configured() is False  # region alone is not enough
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-fake")
    assert M._aws_configured() is True
