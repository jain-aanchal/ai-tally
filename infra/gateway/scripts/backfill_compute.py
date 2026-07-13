#!/usr/bin/env python3
"""Backfill the compute cost layer for a tenant (CTO-143).

Pulls the last N days of cloud compute spend and lands one synthetic ``compute`` span per day, so a
tenant that just enabled the connector doesn't start with an empty Compute column. Idempotent on
``(tenant_id, provider, day)`` — the base connector's emitter skips any day that already has a
synthetic span, so re-running never double-counts.

    uv run python scripts/backfill_compute.py --tenant <uuid> --days 30

Config (provider, credential reference, tag filter) is read from ``tenant_compute_config``; a tenant
without a row is a no-op. A failed fetch records a ``failed`` run and emits NO span (never a guess).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from gateway.config import get_settings
from gateway.connectors.compute import ComputeCostConnector, build_billing_client
from gateway.connectors.config_store import TenantComputeConfigStore
from gateway.store import ClickHouseStore

logger = logging.getLogger("backfill_compute")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the compute cost layer for a tenant.")
    parser.add_argument("--tenant", required=True, help="tenant_id (UUID) to backfill")
    parser.add_argument("--days", type=int, default=30, help="number of days back to backfill")
    parser.add_argument(
        "--end-day",
        default=None,
        help="last day to backfill (YYYY-MM-DD, inclusive); defaults to yesterday (UTC)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.days < 1:
        parser.error("--days must be >= 1")

    end_day = date.fromisoformat(args.end_day) if args.end_day else date.today() - timedelta(days=1)
    start_day = end_day - timedelta(days=args.days - 1)

    settings = get_settings()
    config_store = TenantComputeConfigStore(settings)
    config = config_store.load_config(args.tenant)
    if config is None:
        logger.error("tenant %s has no tenant_compute_config row — nothing to backfill", args.tenant)
        return 2

    store = ClickHouseStore(settings)
    try:
        connector = ComputeCostConnector(
            store=store,
            recorder=config_store,
            billing_client=build_billing_client(config.cloud_provider),
        )
        result = connector.run_backfill(config, start_day=start_day, end_day=end_day)
    finally:
        store.close()

    logger.info(
        "backfill %s provider=%s %s..%s: status=%s spans_emitted=%d total_micro_usd=%d%s",
        args.tenant,
        config.cloud_provider,
        start_day,
        end_day,
        result.status,
        result.spans_emitted,
        result.cost_micro_usd,
        f" error={result.error_message}" if result.error_message else "",
    )
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
