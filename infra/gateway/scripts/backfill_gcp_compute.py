#!/usr/bin/env python3
"""Backfill the GCP compute cost layer for a tenant (CTO-150).

Pulls the last N days of GCP compute spend from the tenant's Cloud Billing BigQuery *export* table
(GCP has no fine-grained REST cost API — the export is the source of truth) and lands one synthetic
``compute`` span per day, so a tenant that just enabled the GCP connector doesn't start with an empty
Compute column.

    uv run python scripts/backfill_gcp_compute.py --tenant <uuid> --days 30

Idempotent on ``(tenant, period, project)``: the base connector's emitter derives a deterministic
span id from ``(tenant, provider, operation, day)`` and skips any day that already has a span, so
re-running the same window never double-counts.

Config (billing-export table, label filter, credential reference) is read from
``tenant_compute_config``; a tenant without a row — or one whose provider isn't ``gcp`` — is a no-op.
The export table falls back to ``TALLY_COMPUTE_GCP_DEFAULT_BILLING_EXPORT_TABLE`` when the tenant row
leaves it blank. A failed fetch records a ``failed`` run and emits NO span (never a guess).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from datetime import date, timedelta

from gateway.config import get_settings
from gateway.connectors.compute import ComputeCostConnector, GcpCloudBillingClient
from gateway.connectors.config_store import TenantComputeConfigStore
from gateway.store import ClickHouseStore

logger = logging.getLogger("backfill_gcp_compute")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the GCP compute cost layer for a tenant.")
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
    if config.cloud_provider != "gcp":
        logger.error(
            "tenant %s is configured for provider=%s, not gcp — use backfill_compute.py",
            args.tenant,
            config.cloud_provider,
        )
        return 2

    # Fall back to the deployment-wide export table when the tenant row leaves it blank.
    if not config.bq_billing_export_table and settings.compute_gcp_default_billing_export_table:
        config = dataclasses.replace(
            config, bq_billing_export_table=settings.compute_gcp_default_billing_export_table
        )

    store = ClickHouseStore(settings)
    try:
        connector = ComputeCostConnector(
            store=store,
            recorder=config_store,
            billing_client=GcpCloudBillingClient(project=settings.compute_gcp_bq_project or None),
        )
        result = connector.run_backfill(config, start_day=start_day, end_day=end_day)
    finally:
        store.close()

    logger.info(
        "backfill %s provider=gcp table=%s %s..%s: status=%s spans_emitted=%d total_micro_usd=%d%s",
        args.tenant,
        config.bq_billing_export_table or "<unset>",
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
