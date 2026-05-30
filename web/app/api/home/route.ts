import { NextResponse } from "next/server";

import { mockDataQuality, mockOutliers, mockRoi, mockSpend } from "@/lib/mock";
import { queryDataQuality, queryOutliers, queryRoi, querySpendSummary } from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [spend, outliers, roi, dq] = await Promise.all([
    querySpendSummary(),
    queryOutliers(),
    queryRoi(),
    queryDataQuality(),
  ]);
  return NextResponse.json({
    spend: spend ?? mockSpend,
    outliers: outliers && outliers.length > 0 ? outliers : mockOutliers,
    roi: roi && roi.length > 0 ? roi : mockRoi,
    dq: dq ?? mockDataQuality,
  });
}
