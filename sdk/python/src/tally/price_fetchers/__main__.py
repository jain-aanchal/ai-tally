# SPDX-License-Identifier: Apache-2.0
"""CLI for the daily price-scraper job (CTO-165).

    python -m tally.price_fetchers [--version V] [--valid-from YYYY-MM-DD]
                                   [--fixtures DIR] [--threshold N]

It fetches (live by default, or from ``--fixtures DIR`` for an offline/deterministic run), proposes
a diff against ``seed_catalog()``, prints the human-readable review artifact, and exits. It never
publishes — a human reviews the artifact and approves separately (see README). Exit code is 2 when
any provider was skipped (so a scheduler/CI can alert), else 0.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from .job import fixture_fetchers, render_review, run_job


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tally.price_fetchers")
    parser.add_argument(
        "--version", default=f"scrape-{date.today().isoformat()}",
        help="candidate catalog version tag (default: scrape-<today>)",
    )
    parser.add_argument(
        "--valid-from", type=_parse_date, default=date.today(),
        help="valid_from date for candidate entries (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--fixtures", default=None,
        help="read recorded fixtures from DIR instead of the network (offline/deterministic run)",
    )
    parser.add_argument(
        "--threshold", type=int, default=10,
        help="large-diff threshold; a larger diff warns and needs ack_large_diff to publish",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    fetchers = fixture_fetchers(args.fixtures) if args.fixtures else None
    result = run_job(
        version=args.version,
        valid_from=args.valid_from,
        fetchers=fetchers,
        large_diff_threshold=args.threshold,
    )
    print(render_review(result))
    return 2 if result.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
