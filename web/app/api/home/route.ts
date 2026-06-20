// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { mockDataQuality, mockOutliers, mockRoi, mockSpend } from "@/lib/mock";
import {
  queryAttribution,
  queryDataQuality,
  queryOutliers,
  queryRoi,
  querySpendSummary,
} from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  // Match the Attribution page's default view so the Home compact table reads
  // the same numbers a user would see on /attribution with no filters set.
  const attributionFilters = { tag: null, provider: null, outcome: "conversion" as const };
  const [spend, outliers, roi, dq, attribution] = await Promise.all([
    querySpendSummary(),
    queryOutliers(),
    queryRoi(),
    queryDataQuality(),
    queryAttribution(attributionFilters),
  ]);
  return NextResponse.json({
    spend: spend ?? mockSpend,
    outliers: outliers && outliers.length > 0 ? outliers : mockOutliers,
    roi: roi && roi.length > 0 ? roi : mockRoi,
    dq: dq ?? mockDataQuality,
    perProviderConversion: attribution?.perProvider ?? [],
  });
}
