// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  type AttributionReport,
  mockReport,
  parseFilters,
} from "@/lib/attribution";
import { queryAttribution } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<NextResponse> {
  const url = new URL(req.url);
  const filters = parseFilters(url.searchParams);
  const live = await queryAttribution(filters);
  // Fall back to the mock report when the query failed (null) OR when there's
  // no chatbot-demo data yet (live but empty). Mirrors the pattern used by
  // /api/agents and /api/cost — and keeps the demo's attribution view useful
  // before the user runs `make chatbot-demo` for the first time.
  const report: AttributionReport =
    live && live.perProvider.length > 0 ? live : mockReport(filters);
  return NextResponse.json(report);
}
