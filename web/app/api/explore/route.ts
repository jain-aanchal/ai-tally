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

import {
  queryCostBreakdownPrior,
  queryCostExplore,
  queryCostSliceTotals,
} from "@/lib/clickhouse";
import { exploreParamsFromFilters } from "@/lib/explore";
import { parseFilters, rangeDays } from "@/lib/filters";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Plain URL API (not NextRequest.nextUrl) so unit tests can pass a bare Request.
  const sp = new URL(req.url).searchParams;
  const state = parseFilters(sp);
  const params = exploreParamsFromFilters(state);

  // One filter-aware fetch feeds all three /cost surfaces (CTO-240): the grouped time series and its
  // breakdown table (queryCostExplore), and the headline tile totals (queryCostSliceTotals). Both
  // honor the FULL filter set including the group-by dimension (CTO-239: exploreParamsFromFilters now
  // carries every filter), so a feature filter narrows the tiles, the chart and the breakdown alike
  // instead of the chart disagreeing with the tiles. Each is independently honest: either can come
  // back null when ClickHouse is unreachable and the page states so rather than zero-filling.
  // The prior-window per-group totals (CTO-244) feed the breakdown table's trend column. It is
  // independently honest: its own null (prior read failed) blanks only the trend cells while the
  // series, totals and the rest of the table still render, so it never gates the page.
  const [series, totals, breakdownPrior] = await Promise.all([
    queryCostExplore(params),
    queryCostSliceTotals(rangeDays(state.range), state.filters),
    queryCostBreakdownPrior(params),
  ]);
  return NextResponse.json({
    source: series === null ? "unavailable" : "live",
    groupBy: params.groupBy,
    series,
    totals,
    breakdownPrior,
  });
}
