// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { costSeries, featureRows, hiddenCostAlerts } from "@/lib/cost";
import {
  queryCostSeries,
  queryFeatureCostRows,
  queryHiddenCostAlerts,
} from "@/lib/clickhouse";
import { parseFilters, rangeDays } from "@/lib/filters";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Optional ?tag=<feature> filter (CTO-104): narrows both the series and the feature-row table to
  // a single feature tag. When the filter is set we never fall back to unfiltered mock — that would
  // misrepresent the filtered view as real data.
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const searchParams = new URL(req.url).searchParams;
  const tag = searchParams.get("tag") ?? "";
  const hasFilter = Boolean(tag);
  // The time-range selector reshapes the headline tiles and the By-feature table (CTO-226): resolve
  // the URL-synced filter state to a day count the ClickHouse-derived window clamps and interpolates.
  // The interactive chart itself is served by /api/explore, so the chart contract is untouched.
  const windowDays = rangeDays(parseFilters(searchParams).range);
  const [series, rows, alerts] = await Promise.all([
    queryCostSeries({ tag }, windowDays),
    queryFeatureCostRows({ tag }, windowDays),
    queryHiddenCostAlerts({ tag }),
  ]);
  return NextResponse.json({
    series: series ?? costSeries,
    featureRows: rows && rows.length > 0 ? rows : hasFilter ? [] : featureRows,
    // Hidden-cost alerts now come from real detection over otel_spans (CTO-122). On the LIVE path
    // we serve queryHiddenCostAlerts' result verbatim — including `[]` (honest-empty: nothing
    // fired). The canned `hiddenCostAlerts` is served ONLY as the ClickHouse-unreachable fallback
    // (query returns null → CI / fresh-clone still renders something), and never under a ?tag=
    // filter (the canned set isn't tag-scoped, so it would misrepresent a filtered view).
    alerts: alerts ?? (hasFilter ? [] : hiddenCostAlerts),
  });
}
