# SPDX-License-Identifier: Apache-2.0
"""CTO-165 fetcher parsing tests. Zero network: fixtures are injected via ``fetch_raw`` and an
autouse guard makes any real socket/urlopen call fail loudly."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tally.price_fetchers import (
    AnthropicPriceFetcher,
    GooglePriceFetcher,
    OpenAIPriceFetcher,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
VERSION = "scrape-test"
VALID_FROM = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any attempt to open a real network connection fails — proves tests never hit the network."""
    import socket
    import urllib.request

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(socket, "socket", _boom)


def _read(name: str):
    def _reader() -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _reader


def _rows(entries) -> set[tuple[str, str, str, str]]:
    return {(e.provider, e.model, e.price_type.value, str(e.price_per_unit)) for e in entries}


def test_openai_parses_exact_rows():
    entries = OpenAIPriceFetcher(fetch_raw=_read("openai.json")).fetch(
        version=VERSION, valid_from=VALID_FROM
    )
    assert _rows(entries) == {
        ("openai", "gpt-4o", "input", "2.55"),
        ("openai", "gpt-4o", "cached_input", "1.28"),
        ("openai", "gpt-4o", "output", "10.10"),
        ("openai", "gpt-4o-mini", "input", "0.16"),
        ("openai", "gpt-4o-mini", "cached_input", "0.08"),
        ("openai", "gpt-4o-mini", "output", "0.64"),
        # gpt-4-turbo has no cached-input tier in the fixture — no CACHED_INPUT row emitted.
        ("openai", "gpt-4-turbo", "input", "10.20"),
        ("openai", "gpt-4-turbo", "output", "30.30"),
    }
    assert all(e.version == VERSION and e.valid_from == VALID_FROM for e in entries)
    assert all(isinstance(e.price_per_unit, Decimal) for e in entries)


def test_anthropic_parses_exact_rows_from_html():
    entries = AnthropicPriceFetcher(fetch_raw=_read("anthropic.html")).fetch(
        version=VERSION, valid_from=VALID_FROM
    )
    assert _rows(entries) == {
        ("anthropic", "claude-sonnet-4-5", "input", "3.03"),
        ("anthropic", "claude-sonnet-4-5", "cached_input", "0.31"),
        ("anthropic", "claude-sonnet-4-5", "output", "15.15"),
        ("anthropic", "claude-haiku-4-5", "input", "1.01"),
        ("anthropic", "claude-haiku-4-5", "cached_input", "0.11"),
        ("anthropic", "claude-haiku-4-5", "output", "5.05"),
        ("anthropic", "claude-opus-4-8", "input", "15.20"),
        ("anthropic", "claude-opus-4-8", "cached_input", "1.55"),
        ("anthropic", "claude-opus-4-8", "output", "75.50"),
    }


def test_google_parses_exact_rows():
    entries = GooglePriceFetcher(fetch_raw=_read("google.json")).fetch(
        version=VERSION, valid_from=VALID_FROM
    )
    assert _rows(entries) == {
        ("google", "gemini-2.5-pro", "input", "1.30"),
        ("google", "gemini-2.5-pro", "cached_input", "0.33"),
        ("google", "gemini-2.5-pro", "output", "11.00"),
        ("google", "gemini-2.5-flash", "input", "0.11"),
        ("google", "gemini-2.5-flash", "cached_input", "0.03"),
        ("google", "gemini-2.5-flash", "output", "0.44"),
    }


def test_fetcher_reraises_and_logs_on_parse_error(caplog):
    import logging

    bad = OpenAIPriceFetcher(fetch_raw=lambda: "not json {{{")
    with caplog.at_level(logging.WARNING, logger="tally.price_fetchers"):
        with pytest.raises(Exception):  # noqa: B017 - json.JSONDecodeError bubbles up
            bad.fetch(version=VERSION, valid_from=VALID_FROM)
    assert any("provider=openai" in r.message for r in caplog.records)


def test_anthropic_row_missing_required_tier_raises():
    html = "<table><tr><td>m</td><td>—</td><td>0.1</td><td>1.0</td></tr></table>"
    f = AnthropicPriceFetcher(fetch_raw=lambda: html)
    with pytest.raises(ValueError, match="missing input/output"):
        f.fetch(version=VERSION, valid_from=VALID_FROM)


def test_fetchers_satisfy_protocol():
    from tally.pricing_scraper import PriceScraper

    # Construction through the real scaffold with fixture-backed fetchers; build_candidate runs.
    scraper = PriceScraper(
        [
            OpenAIPriceFetcher(fetch_raw=_read("openai.json")),
            AnthropicPriceFetcher(fetch_raw=_read("anthropic.html")),
            GooglePriceFetcher(fetch_raw=_read("google.json")),
        ]
    )
    cand = scraper.build_candidate(version=VERSION, valid_from=VALID_FROM)
    assert {e.provider for e in cand} == {"openai", "anthropic", "google"}
