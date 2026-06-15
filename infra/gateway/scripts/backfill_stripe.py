#!/usr/bin/env python3
"""Backfill historical Stripe events into business_events (CTO-110).

Why this exists
---------------
A tenant connecting Stripe for the first time wants to see attribution land *now*, not 30 days
from now. This script fetches the last ``--days`` of supported event types from Stripe's REST API
and feeds them through the same mapper + insert path the webhook uses, so idempotency on
``stripe_event_id`` makes the operation safe to re-run.

It does NOT import the ``stripe`` SDK — we use ``urllib`` against ``api.stripe.com`` directly.
Same reason the gateway doesn't pull the SDK: this is a few hundred lines for one API call, and
the SDK would force a dependency bump for no real win.

Usage::

    python backfill_stripe.py --tenant t-acme --stripe-key sk_live_xxx --days 30
    python backfill_stripe.py --tenant t-acme --stripe-key sk_test_xxx --days 7 --dry-run

The ``--dry-run`` flag prints what *would* be inserted without writing anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

# Add the gateway src to path so we can import the mapper without packaging.
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

from gateway.config import get_settings  # noqa: E402
from gateway.store import ClickHouseStore  # noqa: E402
from gateway.stripe_ingest import (  # noqa: E402
    SUPPORTED_STRIPE_EVENTS,
    hash_customer_email,
    map_stripe_event,
)
from tally.hmac_keys import HmacKeyRegistry  # noqa: E402
from tally.wire import BusinessEvent  # noqa: E402

logger = logging.getLogger("backfill_stripe")

STRIPE_API = "https://api.stripe.com/v1/events"


def fetch_events(
    stripe_key: str,
    *,
    types: list[str],
    since: int,
    page_size: int = 100,
) -> Iterator[dict[str, Any]]:
    """Page through ``GET /v1/events`` with ``starting_after``.

    Stripe paginates oldest-to-newest under ``created[gte]``. We surface one event at a time so
    the caller can decide what to do without holding everything in memory.
    """
    starting_after: str | None = None
    while True:
        params: list[tuple[str, str]] = [
            ("limit", str(page_size)),
            ("created[gte]", str(since)),
        ]
        for t in types:
            params.append(("types[]", t))
        if starting_after:
            params.append(("starting_after", starting_after))
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{STRIPE_API}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {stripe_key}",
                "Stripe-Version": "2024-11-20.acacia",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Don't dump the secret in any error path.
            logger.error("Stripe API %s: %s", exc.code, exc.reason)
            raise SystemExit(1) from exc
        data = body.get("data") or []
        for ev in data:
            yield ev
        if not body.get("has_more") or not data:
            return
        starting_after = data[-1]["id"]


def insert_events(
    events: list[BusinessEvent], tenant: str, store: ClickHouseStore
) -> int:
    return store.insert_business_events(tenant, events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="ai-tally tenant id")
    parser.add_argument("--stripe-key", required=True, help="Stripe secret key (sk_live_... or sk_test_...)")
    parser.add_argument("--days", type=int, default=30, help="how many days of history to fetch (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="don't write to ClickHouse")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.stripe_key.startswith(("sk_live_", "sk_test_")):
        logger.error("--stripe-key must be a Stripe secret key (sk_live_ / sk_test_ prefix)")
        return 2

    since = int(time.time()) - args.days * 86400
    types = sorted(SUPPORTED_STRIPE_EVENTS)
    logger.info(
        "backfill: tenant=%s days=%d types=%s dry_run=%s",
        args.tenant,
        args.days,
        ",".join(types),
        args.dry_run,
    )

    settings = get_settings()
    store = ClickHouseStore(settings) if not args.dry_run else None
    registry = HmacKeyRegistry()

    mapped_total = 0
    inserted_total = 0
    skipped_total = 0
    batch: list[BusinessEvent] = []
    BATCH_SIZE = 250

    def _flush() -> None:
        nonlocal inserted_total
        if not batch:
            return
        if store is not None:
            inserted_total += insert_events(batch, args.tenant, store)
        batch.clear()

    for raw_event in fetch_events(args.stripe_key, types=types, since=since):
        mapped = map_stripe_event(raw_event)
        if mapped is None:
            skipped_total += 1
            continue
        mapped_total += 1
        hashed = hash_customer_email(registry, args.tenant, mapped.customer_email)
        user_id_hash = hashed[0] if hashed else ""
        # ValueType mirrors the webhook handler (commit 2 in this stack).
        value_type = "monetary"
        if mapped.event_name == "refund":
            value_type = "refund"
        elif mapped.event_name == "subscription_renewal":
            value_type = "mrr"
        elif mapped.event_name == "churn":
            value_type = "count"
        ev = BusinessEvent(
            business_event_id=mapped.stripe_event_id,
            event_name=mapped.event_name,
            user_id_hash=user_id_hash,
            occurred_at_ns=mapped.occurred_at_ns,
            value_amount_micro=mapped.value_amount_micro,
            value_currency=mapped.currency,
            value_type=value_type,
            source="stripe",
        )
        if args.dry_run:
            logger.info(
                "[dry-run] %s value=%d %s id=%s",
                ev.event_name,
                ev.value_amount_micro or 0,
                ev.value_currency,
                ev.business_event_id,
            )
        else:
            batch.append(ev)
            if len(batch) >= BATCH_SIZE:
                _flush()

    _flush()
    if store is not None:
        store.close()

    logger.info(
        "backfill done: mapped=%d inserted=%d skipped_unsupported=%d (re-runs are safe — "
        "ReplacingMergeTree on (TenantId, BusinessEventId) collapses duplicates)",
        mapped_total,
        inserted_total,
        skipped_total,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
