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
import { parseFilters, rangeDays } from "@/lib/filters";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  // The Home time-range selector drives every headline (CTO-226): parse the URL-synced filter state
  // and resolve it to a day count the ClickHouse-derived window queries clamp and interpolate.
  const sp = new URL(request.url).searchParams;
  const state = parseFilters(sp);
  const windowDays = rangeDays(state.range);
  // Match the Attribution page's default view so the Home compact table reads
  // the same numbers a user would see on /attribution with no filters set.
  const attributionFilters = { tag: null, provider: null, outcome: "conversion" as const };
  const [spend, outliers, roi, dq, attribution] = await Promise.all([
    querySpendSummary(windowDays),
    queryOutliers(windowDays),
    queryRoi(windowDays),
    queryDataQuality(),
    queryAttribution(attributionFilters, { windowDays }),
  ]);
  return NextResponse.json({
    spend: spend ?? mockSpend,
    outliers: outliers && outliers.length > 0 ? outliers : mockOutliers,
    roi: roi && roi.length > 0 ? roi : mockRoi,
    dq: dq ?? mockDataQuality,
    perProviderConversion: attribution?.perProvider ?? [],
  });
}
