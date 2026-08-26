// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  type AttributionReport,
  mockReport,
  parseFilters,
} from "@/lib/attribution";
import { queryAttribution } from "@/lib/clickhouse";
// The design-foundation FilterBar (CTO-221) writes range/from/to and a `feature` multi-select into
// the same query string. Reading it here (CTO-223) lets the time range and feature filter drive the
// live attribution report, while the attribution-specific tag/provider/outcome params keep working.
import { parseFilters as parseDashboardFilters, rangeDays } from "@/lib/filters";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<NextResponse> {
  const url = new URL(req.url);
  const filters = parseFilters(url.searchParams);
  const dashboard = parseDashboardFilters(url.searchParams);
  const live = await queryAttribution(filters, {
    windowDays: rangeDays(dashboard.range),
    features: dashboard.filters.feature,
  });
  // Fall back to the mock report when the query failed (null) OR when there's
  // no chatbot-demo data yet (live but empty). Mirrors the pattern used by
  // /api/agents and /api/cost — and keeps the demo's attribution view useful
  // before the user runs `make chatbot-demo` for the first time.
  const report: AttributionReport =
    live && live.perProvider.length > 0 ? live : mockReport(filters);
  return NextResponse.json(report);
}
