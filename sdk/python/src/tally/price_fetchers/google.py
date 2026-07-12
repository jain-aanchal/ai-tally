# SPDX-License-Identifier: Apache-2.0
"""Google (Gemini) price fetcher (CTO-165).

Parses Google's published Gemini API pricing, recorded as a small JSON document
(``tests/fixtures/pricing/google.json``). Emits INPUT / CACHED_INPUT / OUTPUT rows per model in
USD per million tokens. Provider id is ``"google"`` (the Gemini API), distinct from a future
Vertex fetcher (out of scope, fast-follow).
"""

from __future__ import annotations

import json
from datetime import date

from tally.pricing import PriceEntry

from ._base import BaseFetcher, ModelRow, rows_to_entries

SOURCE_URL = "https://ai.google.dev/gemini-api/docs/pricing"


def _parse_pricing_json(raw: str) -> list[ModelRow]:
    """Parse Google's ``{"models": [{"name", "input", "cached_input"?, "output"}]}`` shape."""
    doc = json.loads(raw)
    rows: list[ModelRow] = []
    for m in doc["models"]:
        rows.append(
            (
                str(m["name"]),
                str(m["input"]),
                str(m["cached_input"]) if m.get("cached_input") is not None else None,
                str(m["output"]),
            )
        )
    return rows


class GooglePriceFetcher(BaseFetcher):
    """``PriceFetcher`` for Google Gemini, parsing recorded pricing JSON."""

    def __init__(self, fetch_raw=None):
        super().__init__(provider="google", source_url=SOURCE_URL, fetch_raw=fetch_raw)

    def _parse(self, raw: str, *, version: str, valid_from: date) -> list[PriceEntry]:
        rows = _parse_pricing_json(raw)
        return rows_to_entries(self.provider, rows, version=version, valid_from=valid_from)
