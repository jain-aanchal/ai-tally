# SPDX-License-Identifier: Apache-2.0
from datetime import date
from decimal import Decimal

import pytest

from tally.pricing import PriceCatalog, PriceEntry, PriceType, Unit
from tally.pricing_scraper import (
    Approval,
    PriceReviewError,
    PriceScraper,
    diff_entries,
)


def _e(model, pt, rate, version="v2", valid_from=date(2026, 6, 1)):
    return PriceEntry(
        version=version,
        valid_from=valid_from,
        provider="openai",
        model=model,
        price_type=pt,
        unit=Unit.PER_MILLION_TOKENS,
        price_per_unit=Decimal(rate),
    )


class FakeFetcher:
    provider = "openai"

    def __init__(self, entries):
        self._entries = entries

    def fetch(self, *, version, valid_from):
        # re-tag with the requested version
        return [
            PriceEntry(
                version=version,
                valid_from=valid_from,
                provider=e.provider,
                model=e.model,
                price_type=e.price_type,
                unit=e.unit,
                price_per_unit=e.price_per_unit,
            )
            for e in self._entries
        ]


class BoomFetcher:
    provider = "broken"

    def fetch(self, *, version, valid_from):
        raise RuntimeError("scrape failed")


def test_diff_detects_added_changed_removed():
    current = [_e("a", PriceType.INPUT, "1.0"), _e("gone", PriceType.INPUT, "9.0")]
    candidate = [_e("a", PriceType.INPUT, "2.0"), _e("new", PriceType.OUTPUT, "5.0")]
    d = diff_entries(current, candidate)
    assert [x.model for x in d.added] == ["new"]
    assert [x.model for x in d.removed] == ["gone"]
    assert len(d.changed) == 1 and d.changed[0][0].model == "a"
    assert d.magnitude == 3


def test_build_candidate_skips_failing_fetcher():
    scraper = PriceScraper([FakeFetcher([_e("a", PriceType.INPUT, "1.0")]), BoomFetcher()])
    cand = scraper.build_candidate(version="v2", valid_from=date(2026, 6, 1))
    assert len(cand) == 1
    assert cand[0].version == "v2"


def test_publish_requires_approval():
    cat = PriceCatalog([_e("a", PriceType.INPUT, "1.0", version="v1")])
    scraper = PriceScraper([])
    candidate = [_e("a", PriceType.INPUT, "2.0", version="v2")]
    with pytest.raises(PriceReviewError, match="not approved"):
        scraper.publish(cat, candidate, Approval(approved=False))


def test_publish_additive_when_approved():
    # current is an older version; candidate has a later valid_from (newest wins on lookup)
    cat = PriceCatalog(
        [_e("a", PriceType.INPUT, "1.0", version="v1", valid_from=date(2026, 5, 1))]
    )
    scraper = PriceScraper([])
    candidate = [_e("a", PriceType.INPUT, "2.0", version="v2", valid_from=date(2026, 6, 1))]
    diff = scraper.publish(cat, candidate, Approval(approved=True, reviewer="me"))
    assert len(diff.changed) == 1
    # additive: newest valid_from wins on lookup, old version retained
    hit = cat.lookup("openai", "a", PriceType.INPUT, at=date(2026, 7, 1))
    assert hit.price_per_unit == Decimal("2.0")


def test_large_diff_requires_ack():
    cat = PriceCatalog()
    scraper = PriceScraper([], large_diff_threshold=2)
    candidate = [
        _e("m1", PriceType.INPUT, "1"),
        _e("m2", PriceType.INPUT, "1"),
        _e("m3", PriceType.INPUT, "1"),
    ]
    with pytest.raises(PriceReviewError, match="large diff"):
        scraper.publish(cat, candidate, Approval(approved=True))
    # with ack it goes through
    diff = scraper.publish(cat, candidate, Approval(approved=True, ack_large_diff=True))
    assert diff.magnitude == 3


def test_empty_diff_is_noop():
    cat = PriceCatalog([_e("a", PriceType.INPUT, "1.0", version="v1")])
    scraper = PriceScraper([])
    # identical price → empty diff → publish is a no-op even without approval
    candidate = [_e("a", PriceType.INPUT, "1.0", version="v2")]
    diff = scraper.publish(cat, candidate, Approval(approved=False))
    assert diff.is_empty
