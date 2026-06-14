// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { costSeries, featureRows, hiddenCostAlerts } from "@/lib/cost";
import { queryCostSeries, queryFeatureCostRows } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Optional ?tag=<feature> filter (CTO-104): narrows both the series and the feature-row table to
  // a single feature tag. When the filter is set we never fall back to unfiltered mock — that would
  // misrepresent the filtered view as real data.
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const tag = new URL(req.url).searchParams.get("tag") ?? "";
  const hasFilter = Boolean(tag);
  const [series, rows] = await Promise.all([
    queryCostSeries({ tag }),
    queryFeatureCostRows({ tag }),
  ]);
  return NextResponse.json({
    series: series ?? costSeries,
    featureRows: rows && rows.length > 0 ? rows : hasFilter ? [] : featureRows,
    // Hidden-cost alerts are derived from cross-source comparison (not wired live yet); only show
    // the canned alert when we're serving mock data.
    alerts: rows && rows.length > 0 ? [] : hasFilter ? [] : hiddenCostAlerts,
  });
}
