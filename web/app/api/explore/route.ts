// SPDX-License-Identifier: Apache-2.0
// The flexible cost-explore endpoint (CTO-221, D1). Reads the SAME URL query the FilterBar writes
// (range / from / to / groupBy / feature / model / provider / layer / account) so a shared dashboard
// link and its data request are one and the same string. The heavy lifting is queryCostExplore; this
// handler only parses the filters and states honestly when the source is unreachable.
//
// Honest-under-uncertainty: when ClickHouse is unreachable queryCostExplore returns null, and we
// return `source: "unavailable"` with a null series rather than a fabricated or zero-filled chart.
// There is deliberately no mock fallback here (unlike /api/cost): the explorer's whole point is
// arbitrary live slices, and a canned series would misrepresent one.

import { NextResponse } from "next/server";

import { queryCostExplore } from "@/lib/clickhouse";
import { exploreParamsFromFilters } from "@/lib/explore";
import { parseFilters } from "@/lib/filters";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Plain URL API (not NextRequest.nextUrl) so unit tests can pass a bare Request.
  const sp = new URL(req.url).searchParams;
  const state = parseFilters(sp);
  const params = exploreParamsFromFilters(state);

  const series = await queryCostExplore(params);
  if (series === null) {
    return NextResponse.json({ source: "unavailable", groupBy: params.groupBy, series: null });
  }
  return NextResponse.json({ source: "live", groupBy: params.groupBy, series });
}
