// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  LAYERS,
  type CostSeries,
  type FeatureCostRow,
  type HiddenCostAlert,
  costSeries,
  featureRows,
  hiddenCostAlerts,
} from "@/lib/cost";
import {
  queryCostSeries,
  queryFeatureCostRows,
  queryHiddenCostAlerts,
} from "@/lib/clickhouse";
import { type SourceState, readState } from "@/lib/dataState";
import { parseFilters, rangeDays } from "@/lib/filters";
import { sampleDataAllowed } from "@/lib/mock";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface CostSources {
  series: SourceState;
  featureRows: SourceState;
  alerts: SourceState;
}

export interface CostResponse {
  /** null when ClickHouse could not be read. `sources.series` distinguishes that from "no spend". */
  series: CostSeries | null;
  featureRows: FeatureCostRow[];
  alerts: HiddenCostAlert[];
  sources: CostSources;
}

/**
 * A cost series is empty when every day in the window is zero across every layer (#364).
 *
 * The query always returns one point per calendar day, gaps filled with zero layers, so an empty
 * tenant gets a full 30-point series of confident zeros rather than an empty array. The shape says
 * nothing; the figures do.
 */
function seriesIsEmpty(s: CostSeries): boolean {
  return s.days.every((d) => LAYERS.every((l) => d.byLayer[l] === 0));
}

export async function GET(req: Request) {
  // Optional ?tag=<feature> filter (CTO-104): narrows both the series and the feature-row table to
  // a single feature tag.
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const searchParams = new URL(req.url).searchParams;
  const tag = searchParams.get("tag") ?? "";
  // The time-range selector reshapes the headline tiles and the By-feature table (CTO-226): resolve
  // the URL-synced filter state to a day count the ClickHouse-derived window clamps and interpolates.
  // The interactive chart itself is served by /api/explore, so the chart contract is untouched.
  const windowDays = rangeDays(parseFilters(searchParams).range);
  const [series, rows, alerts] = await Promise.all([
    queryCostSeries({ tag }, windowDays),
    queryFeatureCostRows({ tag }, windowDays),
    queryHiddenCostAlerts({ tag }),
  ]);

  // #364: the canned series / feature rows / alerts are a demo build's fixtures now, and nothing
  // else. They used to answer BOTH "ClickHouse is down" and "this tenant has no spend yet", and the
  // second is every new customer. The ?tag= guard that used to be the only protection here is gone
  // because it is no longer the thing standing between a real tenant and fixture numbers.
  // #364 review: fixtures are never filter-scoped, so answering a FILTERED request with the
  // unfiltered canned series makes the filter look broken in a demo build. The agents route already
  // guards this way and has a test for it; matching it keeps the two routes telling one story.
  if (sampleDataAllowed() && !tag) {
    return NextResponse.json({
      series: costSeries,
      featureRows,
      alerts: hiddenCostAlerts,
      sources: { series: "sample", featureRows: "sample", alerts: "sample" },
    } satisfies CostResponse);
  }

  return NextResponse.json({
    series,
    featureRows: rows ?? [],
    // Hidden-cost alerts come from real detection over otel_spans (CTO-122), and `[]` is an honest
    // answer meaning nothing fired: distinct from the null the read returns when it could not run.
    alerts: alerts ?? [],
    sources: {
      series: readState(series, seriesIsEmpty),
      featureRows: readState(rows, (r) => r.length === 0),
      alerts: readState(alerts, (a) => a.length === 0),
    },
  } satisfies CostResponse);
}
