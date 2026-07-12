# SPDX-License-Identifier: Apache-2.0
"""Concrete per-provider price fetchers + the daily scraper job (CTO-165).

Realizes the CTO-53 :mod:`tally.pricing_scraper` scaffold, which shipped with only a fake fetcher.
Each fetcher satisfies the scaffold's :class:`~tally.pricing_scraper.PriceFetcher` Protocol
(``.provider`` + ``.fetch(*, version, valid_from) -> list[PriceEntry]``) and flows through the
existing :class:`~tally.pricing_scraper.PriceScraper` unchanged.

* :class:`OpenAIPriceFetcher` — parses OpenAI API pricing (recorded JSON).
* :class:`AnthropicPriceFetcher` — parses Anthropic pricing (recorded HTML table).
* :class:`GooglePriceFetcher` — parses Google Gemini API pricing (recorded JSON).

Raw content comes through an injectable ``fetch_raw`` callable so tests feed recorded fixtures and
never hit the network; a stdlib-only best-effort live fetch is the default. See :mod:`.job` for the
daily entrypoint, the review artifact, and skipped-provider / large-diff alerting.
"""

from __future__ import annotations

from .anthropic import AnthropicPriceFetcher
from .google import GooglePriceFetcher
from .job import (
    JobResult,
    default_fetchers,
    expected_providers,
    fixture_fetchers,
    present_providers,
    render_review,
    run_job,
    skipped_providers,
)
from .openai import OpenAIPriceFetcher

__all__ = [
    "OpenAIPriceFetcher",
    "AnthropicPriceFetcher",
    "GooglePriceFetcher",
    "JobResult",
    "default_fetchers",
    "fixture_fetchers",
    "expected_providers",
    "present_providers",
    "skipped_providers",
    "render_review",
    "run_job",
]
