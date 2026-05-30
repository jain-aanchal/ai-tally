import { NextResponse } from "next/server";

import { costSeries, featureRows, hiddenCostAlerts } from "@/lib/cost";
import { queryCostSeries, queryFeatureCostRows } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [series, rows] = await Promise.all([queryCostSeries(), queryFeatureCostRows()]);
  return NextResponse.json({
    series: series ?? costSeries,
    featureRows: rows && rows.length > 0 ? rows : featureRows,
    // Hidden-cost alerts are derived from cross-source comparison (not wired live yet); only show
    // the canned alert when we're serving mock data.
    alerts: rows && rows.length > 0 ? [] : hiddenCostAlerts,
  });
}
