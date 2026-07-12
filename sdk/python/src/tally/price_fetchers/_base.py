# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing for concrete price fetchers (CTO-165).

Every provider fetcher parses a recorded pricing SOURCE (HTML/JSON) into
:class:`~tally.pricing.PriceEntry` rows. The raw content is obtained through an *injectable*
callable so tests feed recorded fixtures and never touch the network; a stdlib-only best-effort
live fetch is the default. A parse/network failure logs *which provider/URL* failed and then
re-raises — the scaffold's :meth:`PriceScraper.build_candidate` swallows the exception per-fetcher,
so the log line is the only breadcrumb a silently-skipped provider leaves behind.
"""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tally.pricing import PriceEntry, PriceType, Unit

logger = logging.getLogger("tally.price_fetchers")

#: (model, input, cached_input, output) parsed from a source, rates as USD/million-token strings.
#: A ``None`` cached tier is simply not emitted (a model without a published cached-input price).
ModelRow = tuple[str, str, "str | None", str]

# HTTP timeout for the best-effort live fetch. Tests never reach this path (they inject fetch_raw).
_LIVE_TIMEOUT_S = 15


def live_fetch(url: str) -> str:
    """Best-effort live GET using stdlib only (no new hard HTTP dependency).

    Fetchers default to this when no ``fetch_raw`` is injected. Tests always inject a fixture reader
    instead, so this never runs under pytest.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "tally-price-scraper/1.0"})
    with urllib.request.urlopen(req, timeout=_LIVE_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
        return resp.read().decode("utf-8")


def mtok_entry(
    *,
    provider: str,
    model: str,
    price_type: PriceType,
    usd_per_mtok: str,
    version: str,
    valid_from: date,
) -> PriceEntry:
    """Build a per-million-token entry (mirrors ``pricing._mtok``, no private import)."""
    return PriceEntry(
        version=version,
        valid_from=valid_from,
        provider=provider,
        model=model,
        price_type=price_type,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(usd_per_mtok),
    )


def rows_to_entries(
    provider: str, rows: list[ModelRow], *, version: str, valid_from: date
) -> list[PriceEntry]:
    """Expand ``(model, input, cached, output)`` rows into INPUT/CACHED_INPUT/OUTPUT entries."""
    out: list[PriceEntry] = []
    for model, input_rate, cached_rate, output_rate in rows:
        out.append(
            mtok_entry(
                provider=provider,
                model=model,
                price_type=PriceType.INPUT,
                usd_per_mtok=input_rate,
                version=version,
                valid_from=valid_from,
            )
        )
        if cached_rate is not None:
            out.append(
                mtok_entry(
                    provider=provider,
                    model=model,
                    price_type=PriceType.CACHED_INPUT,
                    usd_per_mtok=cached_rate,
                    version=version,
                    valid_from=valid_from,
                )
            )
        out.append(
            mtok_entry(
                provider=provider,
                model=model,
                price_type=PriceType.OUTPUT,
                usd_per_mtok=output_rate,
                version=version,
                valid_from=valid_from,
            )
        )
    return out


@dataclass(slots=True)
class BaseFetcher:
    """Common fetch/parse/log flow. Subclasses set ``provider``/``source_url`` + ``_parse``.

    ``fetch_raw`` is the injection seam: pass ``lambda: fixture_text`` in tests. When ``None``, the
    fetcher does a best-effort live GET of ``source_url`` (never exercised by the test suite).
    """

    provider: str = ""
    source_url: str = ""
    fetch_raw: Callable[[], str] | None = None

    def _read_raw(self) -> str:
        if self.fetch_raw is not None:
            return self.fetch_raw()
        return live_fetch(self.source_url)

    def _parse(self, raw: str, *, version: str, valid_from: date) -> list[PriceEntry]:
        raise NotImplementedError

    def fetch(self, *, version: str, valid_from: date) -> list[PriceEntry]:
        """Parse the source into entries. Logs which provider/URL failed, then re-raises."""
        try:
            raw = self._read_raw()
            return self._parse(raw, version=version, valid_from=valid_from)
        except Exception:
            logger.warning(
                "price fetch failed for provider=%s url=%s", self.provider, self.source_url
            )
            raise
