#!/usr/bin/env python3
"""Backfill the egress cost layer for a tenant (CTO-144).

Pulls the last N days of bandwidth-out spend for EVERY egress provider the tenant has configured
(Vercel / Cloudflare / AWS) and lands one synthetic ``egress`` span per provider per day, so a tenant
that just enabled the connector doesn't start with an empty Egress column. Idempotent on
``(tenant_id, provider, day)`` — the base connector's emitter skips any day that already has a
synthetic span, so re-running never double-counts, and the distinct-provider span id means multiple
providers sum cleanly.

    uv run python scripts/backfill_egress.py --tenant <uuid> --days 30

Config (provider, credential reference, resource id, usd_per_gb) is read from ``tenant_egress_config``;
a tenant with no rows is a no-op. A failed fetch for one provider records a ``failed`` run for THAT
provider and emits NO span (never a guess), and does not stop the other providers.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from gateway.config import get_settings
from gateway.connectors.config_store import TenantEgressConfigStore
from gateway.connectors.egress import EgressCostConnector, build_egress_client
from gateway.store import ClickHouseStore

logger = logging.getLogger("backfill_egress")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the egress cost layer for a tenant.")
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
    config_store = TenantEgressConfigStore(settings)
    configs = config_store.load_configs(args.tenant)
    if not configs:
        logger.error("tenant %s has no tenant_egress_config rows — nothing to backfill", args.tenant)
        return 2

    store = ClickHouseStore(settings)
    exit_code = 0
    try:
        for config in configs:
            provider = config.cloud_provider
            connector = EgressCostConnector(
                store=store,
                recorder=config_store.recorder_for(provider),
                billing_client=build_egress_client(provider),
            )
            result = connector.run_backfill(config, start_day=start_day, end_day=end_day)
            logger.info(
                "backfill %s provider=%s %s..%s: status=%s spans_emitted=%d total_micro_usd=%d%s",
                args.tenant,
                provider,
                start_day,
                end_day,
                result.status,
                result.spans_emitted,
                result.cost_micro_usd,
                f" error={result.error_message}" if result.error_message else "",
            )
            if result.status != "success":
                exit_code = 1
    finally:
        store.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
