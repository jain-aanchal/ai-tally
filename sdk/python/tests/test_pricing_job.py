# SPDX-License-Identifier: Apache-2.0
"""CTO-165 daily-job tests: proposal flow, skipped-provider + large-diff alerting, review artifact,
and the publish gate. Zero network — everything runs off recorded fixtures."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tally.price_fetchers import (
    AnthropicPriceFetcher,
    GooglePriceFetcher,
    OpenAIPriceFetcher,
    default_fetchers,
    fixture_fetchers,
    render_review,
    run_job,
    skipped_providers,
)
from tally.pricing import PriceCatalog, PriceType, Unit, seed_catalog
from tally.pricing import PriceEntry as PE
from tally.pricing_scraper import Approval, PriceScraper

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
VERSION = "scrape-test"
VALID_FROM = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    import urllib.request

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "socket", _boom)


class _BoomFetcher:
    provider = "google"

    def fetch(self, *, version, valid_from):
        raise RuntimeError("scrape failed")


def _fixture_fetchers():
    return fixture_fetchers(str(FIXTURES))


def _mtok(provider, model, pt, rate):
    return PE(
        version="v-old",
        valid_from=date(2026, 5, 1),
        provider=provider,
        model=model,
        price_type=pt,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(rate),
    )


def _openai_only_catalog() -> PriceCatalog:
    # Only the OpenAI models the fixture covers, at OLD rates → deterministic changed/added/removed.
    return PriceCatalog(
        [
            _mtok("openai", "gpt-4o", PriceType.INPUT, "2.50"),
            _mtok("openai", "gpt-4o", PriceType.CACHED_INPUT, "1.25"),
            _mtok("openai", "gpt-4o", PriceType.OUTPUT, "10.00"),
            _mtok("openai", "gpt-4o-mini", PriceType.INPUT, "0.15"),
            _mtok("openai", "gpt-4o-mini", PriceType.CACHED_INPUT, "0.075"),
            _mtok("openai", "gpt-4o-mini", PriceType.OUTPUT, "0.60"),
            _mtok("openai", "gpt-4-turbo", PriceType.INPUT, "10.00"),
            _mtok("openai", "gpt-4-turbo", PriceType.OUTPUT, "30.00"),
        ]
    )


def test_default_fetchers_cover_three_providers():
    assert {f.provider for f in default_fetchers()} == {"openai", "anthropic", "google"}
    assert [type(f) for f in default_fetchers()] == [
        OpenAIPriceFetcher,
        AnthropicPriceFetcher,
        GooglePriceFetcher,
    ]


def test_run_job_changed_and_added_against_small_catalog():
    result = run_job(
        version=VERSION,
        valid_from=VALID_FROM,
        catalog=_openai_only_catalog(),
        fetchers=_fixture_fetchers(),
    )
    d = result.diff
    assert not result.skipped
    # OpenAI models exist in the catalog → rate moves land as CHANGED.
    changed = {(o.model, o.price_type.value, str(o.price_per_unit), str(n.price_per_unit))
               for o, n in d.changed}
    assert ("gpt-4o", "input", "2.50", "2.55") in changed
    assert ("gpt-4o-mini", "output", "0.60", "0.64") in changed
    # Anthropic + Google are absent from the catalog → ADDED.
    added = {(e.provider, e.model, e.price_type.value) for e in d.added}
    assert ("google", "gemini-2.5-pro", "input") in added
    assert ("anthropic", "claude-opus-4-8", "output") in added
    assert not d.removed  # candidate covers every catalog key


def test_skipped_provider_surfaced_and_logged(caplog):
    fetchers = [
        OpenAIPriceFetcher(fetch_raw=(FIXTURES / "openai.json").read_text),
        AnthropicPriceFetcher(fetch_raw=(FIXTURES / "anthropic.html").read_text),
        _BoomFetcher(),  # google raises → contributes no rows
    ]
    with caplog.at_level(logging.WARNING, logger="tally.price_fetchers"):
        result = run_job(
            version=VERSION,
            valid_from=VALID_FROM,
            catalog=_openai_only_catalog(),
            fetchers=fetchers,
        )
    assert result.skipped == {"google"}
    assert any("provider=google" in r.message and "skipped" in r.message for r in caplog.records)
    # helper is independently testable
    cand = PriceScraper(fetchers).build_candidate(version=VERSION, valid_from=VALID_FROM)
    assert skipped_providers(fetchers, cand) == {"google"}


def test_large_diff_emits_structured_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tally.price_fetchers"):
        result = run_job(
            version=VERSION,
            valid_from=VALID_FROM,
            catalog=_openai_only_catalog(),
            fetchers=_fixture_fetchers(),
            large_diff_threshold=2,
        )
    assert result.is_large_diff
    assert any("large price diff" in r.message for r in caplog.records)


def test_render_review_has_sections_and_skipped_line():
    fetchers = [
        OpenAIPriceFetcher(fetch_raw=(FIXTURES / "openai.json").read_text),
        AnthropicPriceFetcher(fetch_raw=(FIXTURES / "anthropic.html").read_text),
        _BoomFetcher(),
    ]
    result = run_job(
        version=VERSION,
        valid_from=VALID_FROM,
        catalog=_openai_only_catalog(),
        fetchers=fetchers,
    )
    text = render_review(result)
    assert "## Added" in text and "## Changed" in text and "## Removed" in text
    assert "SKIPPED PROVIDERS" in text and "google" in text
    assert "gpt-4o" in text


def test_publish_gate_applies_fetched_rates(caplog):
    catalog = _openai_only_catalog()
    fetchers = _fixture_fetchers()
    scraper = PriceScraper(fetchers)
    candidate = scraper.build_candidate(version=VERSION, valid_from=VALID_FROM)
    # Job proposes; a human approves explicitly (large diff → ack required).
    scraper.publish(catalog, candidate, Approval(approved=True, reviewer="me", ack_large_diff=True))
    # Published fetched rate now wins on lookup (newest valid_from).
    hit = catalog.lookup("openai", "gpt-4o", PriceType.INPUT, at=date(2026, 7, 2))
    assert hit.price_per_unit == Decimal("2.55")
    # Gemini, absent from THIS catalog, is now present after publish (additive supersession).
    gem = catalog.lookup("google", "gemini-2.5-pro", PriceType.OUTPUT, at=date(2026, 7, 2))
    assert gem is not None and gem.price_per_unit == Decimal("11.00")


def test_seed_catalog_untouched():
    # Guard: CTO-165 must NOT edit seed_catalog(). Original seed rates remain (NOT our fixtures).
    cat = seed_catalog()
    assert cat.lookup(
        "openai", "gpt-4o", PriceType.INPUT, at=date(2026, 7, 1)
    ).price_per_unit == Decimal("2.50")  # seed value, not the fixture's 2.55
    # Gemini is seeded (CTO-149) at the seed rate, not our fixture rate — proves no seed edit.
    assert cat.lookup(
        "google", "gemini-2.5-pro", PriceType.INPUT, at=date(2026, 7, 1)
    ).price_per_unit == Decimal("1.25")  # seed value, not the fixture's 1.30


def test_run_job_defaults_to_seed_catalog_and_runs_offline():
    # Against the real seed catalog with fixture fetchers: fetched Gemini rates supersede the
    # seed's [unverified] Gemini rates, so they land as CHANGED (both models are already seeded).
    result = run_job(version=VERSION, valid_from=VALID_FROM, fetchers=_fixture_fetchers())
    changed = {
        (o.provider, o.model, o.price_type.value, str(o.price_per_unit), str(n.price_per_unit))
        for o, n in result.diff.changed
    }
    assert ("google", "gemini-2.5-pro", "input", "1.25", "1.30") in changed
    assert not result.skipped
