# SPDX-License-Identifier: Apache-2.0
"""Daily price-scraper job (CTO-165).

Wires the concrete fetchers into the CTO-53 :class:`~tally.pricing_scraper.PriceScraper` and turns a
run into a *human-readable review artifact*. The job only PROPOSES — publishing stays behind
:class:`~tally.pricing_scraper.Approval` (a human approves; nothing auto-publishes).

Two surfacing concerns, because :meth:`PriceScraper.build_candidate` swallows per-fetcher
exceptions (so a broken provider goes silent):

* **Skipped providers** — :func:`skipped_providers` diffs EXPECTED vs. PRESENT providers in the
  candidate set. A provider that raised (network/parse) contributes no rows, so it shows up here and
  gets logged as a WARNING.
* **Large diff** — when ``diff.magnitude`` exceeds the scraper's ``large_diff_threshold`` the job
  emits a structured WARNING (the ops path alerts on it). Publish still requires
  ``Approval(ack_large_diff=True)``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from tally.pricing import PriceCatalog, PriceEntry, seed_catalog
from tally.pricing_scraper import CatalogDiff, PriceFetcher, PriceScraper

from .anthropic import AnthropicPriceFetcher
from .google import GooglePriceFetcher
from .openai import OpenAIPriceFetcher

logger = logging.getLogger("tally.price_fetchers")


def default_fetchers() -> list[PriceFetcher]:
    """The real fetchers, live by default (best-effort network fetch of each provider source)."""
    return [OpenAIPriceFetcher(), AnthropicPriceFetcher(), GooglePriceFetcher()]


def expected_providers(fetchers: Sequence[PriceFetcher]) -> set[str]:
    """Providers we EXPECT a candidate row set to cover — one per configured fetcher."""
    return {f.provider for f in fetchers}


def present_providers(candidate: Sequence[PriceEntry]) -> set[str]:
    """Providers actually PRESENT in the candidate set (a fetcher that raised contributes none)."""
    return {e.provider for e in candidate}


def skipped_providers(
    fetchers: Sequence[PriceFetcher], candidate: Sequence[PriceEntry]
) -> set[str]:
    """Expected-but-absent providers — the silently-skipped set. Testable and logged by the job."""
    return expected_providers(fetchers) - present_providers(candidate)


@dataclass(frozen=True, slots=True)
class JobResult:
    """Outcome of a proposal run: the diff, the skipped providers, and the large-diff flag."""

    version: str
    valid_from: date
    diff: CatalogDiff
    skipped: set[str] = field(default_factory=set)
    large_diff_threshold: int = 10

    @property
    def is_large_diff(self) -> bool:
        return self.diff.magnitude > self.large_diff_threshold


def run_job(
    *,
    version: str,
    valid_from: date,
    catalog: PriceCatalog | None = None,
    fetchers: Sequence[PriceFetcher] | None = None,
    large_diff_threshold: int = 10,
) -> JobResult:
    """Fetch → build candidate → propose vs. ``catalog`` (default :func:`seed_catalog`). No publish.

    Logs a WARNING per skipped provider and a structured WARNING when the diff is large, so a
    silently-missing provider or an oversized change is visible to the ops path.
    """
    catalog = catalog if catalog is not None else seed_catalog()
    fetchers = list(fetchers) if fetchers is not None else default_fetchers()
    scraper = PriceScraper(list(fetchers), large_diff_threshold=large_diff_threshold)

    candidate = scraper.build_candidate(version=version, valid_from=valid_from)
    diff = scraper.propose(catalog, candidate)
    skipped = skipped_providers(fetchers, candidate)

    for provider in sorted(skipped):
        logger.warning("price fetcher produced no rows (skipped) provider=%s version=%s",
                       provider, version)

    result = JobResult(
        version=version,
        valid_from=valid_from,
        diff=diff,
        skipped=skipped,
        large_diff_threshold=large_diff_threshold,
    )
    if result.is_large_diff:
        logger.warning(
            "large price diff magnitude=%d threshold=%d added=%d changed=%d removed=%d "
            "version=%s (publish requires ack_large_diff=True)",
            diff.magnitude,
            large_diff_threshold,
            len(diff.added),
            len(diff.changed),
            len(diff.removed),
            version,
        )
    return result


def _unit_suffix(e: PriceEntry) -> str:
    from tally.pricing import Unit

    return "/Mtok" if e.unit is Unit.PER_MILLION_TOKENS else "/call"


def _fmt_entry(e: PriceEntry) -> str:
    return f"{e.provider}/{e.model} {e.price_type.value} = ${e.price_per_unit}{_unit_suffix(e)}"


def render_review(result: JobResult) -> str:
    """Render a human-readable review artifact: added/changed/removed + skipped providers.

    This is what a human reads before approving. Publishing is a separate, explicit step.
    """
    d = result.diff
    lines: list[str] = []
    lines.append(f"# Price scraper review — candidate version {result.version}")
    lines.append(f"valid_from: {result.valid_from.isoformat()}")
    lines.append(
        f"diff magnitude: {d.magnitude} "
        f"(added {len(d.added)}, changed {len(d.changed)}, removed {len(d.removed)})"
    )
    if result.is_large_diff:
        lines.append(
            f"** LARGE DIFF ** exceeds threshold {result.large_diff_threshold}; "
            f"approval must set ack_large_diff=True"
        )
    if result.skipped:
        lines.append(f"** SKIPPED PROVIDERS **: {', '.join(sorted(result.skipped))} "
                     f"(fetch/parse failed — not updated this run)")
    else:
        lines.append("skipped providers: none")

    lines.append("")
    lines.append(f"## Added ({len(d.added)})")
    for e in sorted(d.added, key=_fmt_entry):
        lines.append(f"  + {_fmt_entry(e)}")

    lines.append("")
    lines.append(f"## Changed ({len(d.changed)})")
    for old, new in sorted(d.changed, key=lambda p: _fmt_entry(p[1])):
        lines.append(
            f"  ~ {new.provider}/{new.model} {new.price_type.value}: "
            f"${old.price_per_unit} -> ${new.price_per_unit} /Mtok"
        )

    lines.append("")
    lines.append(f"## Removed ({len(d.removed)})")
    for e in sorted(d.removed, key=_fmt_entry):
        lines.append(f"  - {_fmt_entry(e)}")

    lines.append("")
    if d.is_empty:
        lines.append("No changes — nothing to approve.")
    else:
        lines.append("To publish: pass an Approval(approved=True, reviewer=..., "
                     "ack_large_diff=<bool>) to PriceScraper.publish().")
    return "\n".join(lines)


def fixture_fetchers(fixture_dir: str) -> list[PriceFetcher]:
    """Real fetchers wired to recorded fixtures instead of the network (offline demo runs + tests).

    ``fixture_dir`` holds ``openai.json``, ``anthropic.html``, ``google.json``.
    """
    import os

    def reader(name: str) -> Callable[[], str]:
        path = os.path.join(fixture_dir, name)

        def _read() -> str:
            with open(path, encoding="utf-8") as fh:
                return fh.read()

        return _read

    return [
        OpenAIPriceFetcher(fetch_raw=reader("openai.json")),
        AnthropicPriceFetcher(fetch_raw=reader("anthropic.html")),
        GooglePriceFetcher(fetch_raw=reader("google.json")),
    ]
