# SPDX-License-Identifier: Apache-2.0
"""Google Gemini / Vertex AI as a first-class LLM cost provider (CTO-149).

Covers the three things the ticket asks the SDK side to prove:
  1. the seed catalog prices the Gemini lineup (non-zero, correct against the seeded rates);
  2. ``record_llm_call(provider="google", ...)`` costs from the catalog and stamps
     ``gen_ai.system="google"`` on a conformant span — no provider allowlist to trip over;
  3. model discovery classifies a mocked Google model list into flash/pro and fails soft.
"""

from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

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

FIXTURES = Path(__file__).parent / "fixtures"
AT = date(2026, 6, 1)


# --- Price catalog ------------------------------------------------------------


def test_gemini_3_flash_priced_correctly() -> None:
    cat = seed_catalog()
    # 1M input @ $0.30/MTok + 1M output @ $2.50/MTok = $2.80 = 2_800_000 micro-USD.
    cost, version = compute_cost_micro_usd(
        cat, "google", "gemini-3-flash", Usage(1_000_000, 1_000_000), at=AT
    )
    assert cost == 2_800_000
    assert version  # a real catalog version, not the empty "partial price" sentinel


def test_gemini_2_5_pro_priced_correctly() -> None:
    cat = seed_catalog()
    # 1M input @ $1.25/MTok + 1M output @ $10.00/MTok = $11.25 = 11_250_000 micro-USD.
    cost, version = compute_cost_micro_usd(
        cat, "google", "gemini-2.5-pro", Usage(1_000_000, 1_000_000), at=AT
    )
    assert cost == 11_250_000
    assert version


def test_gemini_2_5_flash_has_cached_tier() -> None:
    cat = seed_catalog()
    cached = cat.lookup("google", "gemini-2.5-flash", PriceType.CACHED_INPUT, at=AT)
    standard = cat.lookup("google", "gemini-2.5-flash", PriceType.INPUT, at=AT)
    assert cached is not None and standard is not None
    # cached read tier must be cheaper than the standard input tier
    assert cached.price_per_unit < standard.price_per_unit


def test_gemini_cost_is_nonzero_for_small_usage() -> None:
    cat = seed_catalog()
    for model in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash"):
        cost, version = compute_cost_micro_usd(cat, "google", model, Usage(1000, 250), at=AT)
        assert cost > 0, model
        assert version, model


def test_gemini_rates_are_decimal() -> None:
    # Money is Decimal, never float (guards against a stray float rate slipping into the seed).
    cat = seed_catalog()
    entry = cat.lookup("google", "gemini-3-flash", PriceType.OUTPUT, at=AT)
    assert entry is not None
    assert isinstance(entry.price_per_unit, Decimal)


# --- Provider path (record_llm_call) ------------------------------------------


def _client(**kw) -> TallyClient:
    return TallyClient(catalog=seed_catalog(), **kw)


def test_record_llm_call_google_costs_from_catalog() -> None:
    exporter = MemoryExporter()
    client = _client(exporter=exporter, sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with with_trace_context(trace_id="t1", feature_tag="compare", session_id="s1"):
        result = client.record_llm_call(
            provider="google",
            model="gemini-3-flash",
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
    assert result.kept is True
    assert result.cost_micro_usd == 2_800_000
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    assert validate_span_attributes(span) == []
    assert span[GenAI.SYSTEM] == "google"
    assert span[GenAI.COST_ESTIMATED_MICRO_USD] == 2_800_000


def test_record_llm_call_google_maps_cached_tokens() -> None:
    # cachedContentTokenCount -> cached_input_tokens; billed at the cheaper cached rate.
    client = _client(sampler=Sampler(SamplingConfig(body_rate=1.0)))
    with with_trace_context(trace_id="t2"):
        full = client.record_llm_call(
            provider="google",
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=1_000_000, output_tokens=0),
        )
        cached = client.record_llm_call(
            provider="google",
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
        )
    assert full.cost_micro_usd is not None and cached.cost_micro_usd is not None
    # A fully-cached prompt costs strictly less than the same prompt uncached.
    assert cached.cost_micro_usd < full.cost_micro_usd


# --- Model discovery ----------------------------------------------------------


def _fake_urlopen_factory(payloads_by_url: dict[str, dict]):
    class _Resp:
        def __init__(self, body: bytes):
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for prefix, payload in payloads_by_url.items():
            if url.startswith(prefix):
                return _Resp(json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"unexpected URL {url}")

    return fake_urlopen


def test_classify_gemini_families() -> None:
    assert M.classify_family("gemini-2.5-flash") == "flash"
    assert M.classify_family("gemini-3-flash") == "flash"
    assert M.classify_family("gemini-2.5-pro") == "pro"


def test_fetch_google_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "google_models.json").read_text())
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"https://generativelanguage.googleapis.com/v1beta/models": payload}
        ),
    )
    out = M.fetch_google_models("fake-key")
    ids = {m.id for m in out}
    # "models/" prefix stripped to the bare catalog/Compare slug.
    assert "gemini-2.5-flash" in ids
    assert "gemini-2.5-pro" in ids
    assert "gemini-3-flash" in ids
    assert all(m.provider == "google" for m in out)
    # Families are classified so latest("google", ...) can resolve them.
    flash = M.latest("google", "flash", out)
    pro = M.latest("google", "pro", out)
    assert flash is not None and flash.family == "flash"
    assert pro is not None and pro.id == "gemini-2.5-pro"


def test_discover_includes_google_when_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads((FIXTURES / "google_models.json").read_text())
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"https://generativelanguage.googleapis.com/v1beta/models": payload}
        ),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(cache_path=tmp_path / "models.json")
    google_ids = {m.id for m in result if m.provider == "google"}
    assert "gemini-3-flash" in google_ids


def test_discover_uses_gemini_api_key_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads((FIXTURES / "google_models.json").read_text())
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"https://generativelanguage.googleapis.com/v1beta/models": payload}
        ),
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(cache_path=tmp_path / "models.json")
    assert any(m.provider == "google" for m in result)


def test_discover_google_fails_soft_on_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Google unreachable + no cache → empty list, no crash (fail-soft, like the others).
    def boom(req, timeout=None):  # noqa: ARG001
        raise TimeoutError("gemini is down")

    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(cache_path=tmp_path / "absent.json")
    assert result == []


def test_discover_google_fails_soft_on_malformed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A body missing the "models" key must not crash — tolerant parsing skips Google.
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory(
            {"https://generativelanguage.googleapis.com/v1beta/models": {"unexpected": []}}
        ),
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.setenv("TALLY_MODELS_REFRESH", "1")

    result = M.discover_models(cache_path=tmp_path / "absent.json")
    # No google models, but no crash either.
    assert [m for m in result if m.provider == "google"] == []
