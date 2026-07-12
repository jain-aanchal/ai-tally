// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { costSeries, featureRows, hiddenCostAlerts } from "@/lib/cost";
import {
  queryCostSeries,
  queryFeatureCostRows,
  queryHiddenCostAlerts,
} from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Optional ?tag=<feature> filter (CTO-104): narrows both the series and the feature-row table to
  // a single feature tag. When the filter is set we never fall back to unfiltered mock — that would
  // misrepresent the filtered view as real data.
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const tag = new URL(req.url).searchParams.get("tag") ?? "";
  const hasFilter = Boolean(tag);
  const [series, rows, alerts] = await Promise.all([
    queryCostSeries({ tag }),
    queryFeatureCostRows({ tag }),
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
