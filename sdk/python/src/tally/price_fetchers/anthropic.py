# SPDX-License-Identifier: Apache-2.0
"""Anthropic price fetcher (CTO-165).

Parses Anthropic's published pricing table, recorded as a small HTML fragment
(``tests/fixtures/pricing/anthropic.html``). Demonstrates the HTML-source path (OpenAI/Google use
JSON). Uses the stdlib :mod:`html.parser` — no new dependency.

The recorded table has a header row plus one row per model:
``model | input | cached input | output`` (rates USD per million tokens). ``CACHED_INPUT`` maps to
the cache-read tier, matching the seed catalog's convention. A cell of ``—`` means "no such tier".
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser

from tally.pricing import PriceEntry

from ._base import BaseFetcher, ModelRow, rows_to_entries

SOURCE_URL = "https://www.anthropic.com/pricing"

# Cells that denote "this tier does not exist for this model".
_ABSENT = {"", "—", "-", "n/a", "N/A"}


class _PricingTableParser(HTMLParser):
    """Collect ``<tr>``/``<td>``/``<th>`` text from the first pricing table into a grid of rows."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_cell = False
        self._cur_row: list[str] | None = None
        self._cur_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cur_row is not None:
            self._cur_row.append("".join(self._cur_cell).strip())
            self._in_cell = False
        elif tag == "tr" and self._cur_row is not None:
            self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_cell.append(data)


def _cell_or_none(value: str) -> str | None:
    return None if value.strip() in _ABSENT else value.strip()


def _parse_pricing_html(raw: str) -> list[ModelRow]:
    """Parse the recorded HTML pricing table into ``(model, input, cached, output)`` rows.

    Skips the header row (``model`` label in the first cell) and any short/blank row.
    """
    parser = _PricingTableParser()
    parser.feed(raw)
    rows: list[ModelRow] = []
    for cells in parser.rows:
        if len(cells) < 4:
            continue
        model = cells[0].strip()
        if not model or model.lower() == "model":
            continue  # header
        input_rate = _cell_or_none(cells[1])
        cached_rate = _cell_or_none(cells[2])
        output_rate = _cell_or_none(cells[3])
        if input_rate is None or output_rate is None:
            # INPUT/OUTPUT are required tiers; a row missing them is malformed for our purposes.
            raise ValueError(f"anthropic row for {model!r} missing input/output rate")
        rows.append((model, input_rate, cached_rate, output_rate))
    if not rows:
        raise ValueError("anthropic pricing table yielded no model rows")
    return rows


class AnthropicPriceFetcher(BaseFetcher):
    """``PriceFetcher`` for Anthropic, parsing a recorded HTML pricing table."""

    def __init__(self, fetch_raw=None):
        super().__init__(provider="anthropic", source_url=SOURCE_URL, fetch_raw=fetch_raw)

    def _parse(self, raw: str, *, version: str, valid_from: date) -> list[PriceEntry]:
        rows = _parse_pricing_html(raw)
        return rows_to_entries(self.provider, rows, version=version, valid_from=valid_from)
