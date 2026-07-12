# SPDX-License-Identifier: Apache-2.0
"""OpenAI price fetcher (CTO-165).

Parses OpenAI's published API pricing, recorded as a small JSON document
(``tests/fixtures/pricing/openai.json``). Emits INPUT / CACHED_INPUT / OUTPUT rows per model in
USD per million tokens.
"""

from __future__ import annotations

import json
from datetime import date

from tally.pricing import PriceEntry

from ._base import BaseFetcher, ModelRow, rows_to_entries

SOURCE_URL = "https://openai.com/api/pricing/"


def _parse_pricing_json(raw: str) -> list[ModelRow]:
    """Parse the ``{"models": [{"model", "input", "cached_input"?, "output"}]}`` pricing shape."""
    doc = json.loads(raw)
    rows: list[ModelRow] = []
    for m in doc["models"]:
        rows.append(
            (
                str(m["model"]),
                str(m["input"]),
                str(m["cached_input"]) if m.get("cached_input") is not None else None,
                str(m["output"]),
            )
        )
    return rows


class OpenAIPriceFetcher(BaseFetcher):
    """:class:`~tally.pricing_scraper.PriceFetcher` for OpenAI, parsing recorded pricing JSON."""

    def __init__(self, fetch_raw=None):
        super().__init__(provider="openai", source_url=SOURCE_URL, fetch_raw=fetch_raw)

    def _parse(self, raw: str, *, version: str, valid_from: date) -> list[PriceEntry]:
        rows = _parse_pricing_json(raw)
        return rows_to_entries(self.provider, rows, version=version, valid_from=valid_from)
