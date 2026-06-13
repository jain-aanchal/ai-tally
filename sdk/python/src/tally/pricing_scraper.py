# SPDX-License-Identifier: Apache-2.0
"""Price scraper scaffold — pluggable fetchers, diff, and a human-review gate.

Implements CTO-53.

A stale catalog silently corrupts every cost number, so price updates are:
1. fetched from pluggable per-provider :class:`PriceFetcher` sources into a *candidate* version,
2. diffed against the current catalog,
3. published only behind an explicit human :class:`Approval` (the review gate). New versions are
   additive — old versions are retained so historical cost stays recomputable (CTO-52).

This is the scaffold (the actual scraping of provider pages lives in concrete fetchers); tested
here with a fake fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from tally.pricing import PriceCatalog, PriceEntry


class PriceFetcher(Protocol):
    """A source of candidate prices for one provider. Implementations do the actual scraping."""

    provider: str

    def fetch(self, *, version: str, valid_from: date) -> list[PriceEntry]: ...


def _key(e: PriceEntry) -> tuple[str, str, str]:
    return (e.provider, e.model, e.price_type.value)


@dataclass(frozen=True, slots=True)
class CatalogDiff:
    added: list[PriceEntry] = field(default_factory=list)
    changed: list[tuple[PriceEntry, PriceEntry]] = field(default_factory=list)  # (old, new)
    removed: list[PriceEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    @property
    def magnitude(self) -> int:
        return len(self.added) + len(self.changed) + len(self.removed)


def diff_entries(current: list[PriceEntry], candidate: list[PriceEntry]) -> CatalogDiff:
    """Diff candidate vs. current by (provider, model, price_type)."""
    cur = {_key(e): e for e in current}
    cand = {_key(e): e for e in candidate}
    added = [cand[k] for k in cand.keys() - cur.keys()]
    removed = [cur[k] for k in cur.keys() - cand.keys()]
    changed = [
        (cur[k], cand[k])
        for k in cur.keys() & cand.keys()
        if cur[k].price_per_unit != cand[k].price_per_unit
    ]
    return CatalogDiff(added=added, changed=changed, removed=removed)


@dataclass(frozen=True, slots=True)
class Approval:
    approved: bool
    reviewer: str = ""
    #: explicit acknowledgement required when a diff exceeds ``large_diff_threshold``
    ack_large_diff: bool = False


class PriceReviewError(Exception):
    """Raised when a candidate cannot be published under the review gate."""


@dataclass(slots=True)
class PriceScraper:
    """Runs fetchers, proposes a diff, and publishes behind a review gate."""

    fetchers: list[PriceFetcher]
    large_diff_threshold: int = 10

    def build_candidate(self, *, version: str, valid_from: date) -> list[PriceEntry]:
        """Fetch all sources into a candidate set tagged with ``version``.

        A fetcher that raises does not abort the run — its failure is skipped (and the missing
        provider simply isn't updated). Callers should monitor for missing providers.
        """
        out: list[PriceEntry] = []
        for f in self.fetchers:
            try:
                out.extend(f.fetch(version=version, valid_from=valid_from))
            except Exception:  # noqa: BLE001 - one bad source shouldn't sink the run
                continue
        return out

    def propose(self, current: PriceCatalog, candidate: list[PriceEntry]) -> CatalogDiff:
        # diff against the catalog's public entries (latest of each key, regardless of version)
        current_entries = list(current._entries)  # noqa: SLF001 - same package
        return diff_entries(current_entries, candidate)

    def publish(
        self,
        current: PriceCatalog,
        candidate: list[PriceEntry],
        approval: Approval,
    ) -> CatalogDiff:
        """Publish the candidate into ``current`` (additive) iff approved.

        Raises :class:`PriceReviewError` when not approved, or when the diff is large and the
        reviewer didn't explicitly acknowledge it.
        """
        diff = self.propose(current, candidate)
        if diff.is_empty:
            return diff
        if not approval.approved:
            raise PriceReviewError("candidate not approved by a reviewer")
        if diff.magnitude > self.large_diff_threshold and not approval.ack_large_diff:
            raise PriceReviewError(
                f"large diff ({diff.magnitude} changes) requires ack_large_diff=True"
            )
        for entry in candidate:
            current.add(entry)  # additive: old versions retained
        return diff
