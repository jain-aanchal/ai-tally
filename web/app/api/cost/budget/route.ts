// SPDX-License-Identifier: Apache-2.0
// Month-to-date actual versus budget for /cost (CTO-209, F5).
//
// WHY THIS IS ITS OWN ROUTE AND NOT PART OF /api/cost. The cost page live-polls /api/cost every 5
// seconds (`useLivePoll`, NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS). This payload costs two more
// ClickHouse reads plus a gateway round trip, and none of it changes at that cadence: a budget is
// edited by a human maybe monthly, and the settled window advances once a day. Folding it into the
// polled route would multiply that work by 720 an hour to redraw an identical section. So it is
// fetched once per page render, server side, and the section states the days it covers instead of
// pretending to be live.
//
// CTO-210 (F6) adds the burn-down section to the same payload rather than a second route. Both
// sections are derived from ONE `querySettledCostSeries` result, so the measured month-to-date
// figure and the projection can never be reading two different windows, and the forecast costs no
// extra ClickHouse work at all: `burndownSection` is pure arithmetic over the series already read
// for the comparison.
//
// The comparison is always TENANT-WIDE, even under /cost?tag=<feature>. The budget scopes in
// CTO-205 include 'feature', but wiring the tag filter to a feature-scoped budget is the
// scoped-budget phase of docs/spend-forecasting-scope.md, not this ticket, and silently comparing
// one feature's spend against the whole tenant's budget would be worse than either.

import { NextResponse } from "next/server";

import { budgetVsActual } from "@/lib/budgetVsActual";
import { burndownSection } from "@/lib/burndown";
import { querySettledCostSeries } from "@/lib/clickhouse";
import { fetchTenantBudgets } from "@/lib/tenantBudgetsClient";

import type { CostBudgetPayload } from "@/app/cost/BurndownCard";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [series, budgets] = await Promise.all([
    querySettledCostSeries({ kind: "tenant" }),
    fetchTenantBudgets(),
  ]);

  // No mock fallback on this surface, unlike the rest of /cost. Everywhere else a canned series
  // keeps a fresh clone looking alive; here it would put invented spend next to a budget somebody
  // really set and produce a variance that is pure fiction. "We cannot read the spend" is the only
  // honest answer, and the section renders it as a blank with this reason attached.
  // The same reason blanks both sections: a projection built on invented spend and compared against
  // a budget somebody really set would be fiction with a date attached, which is worse here than on
  // the measured card because a reader would act on it days in advance.
  if (!series) {
    const reason = "spend data is unavailable: ClickHouse is unreachable from the dashboard";
    const payload: CostBudgetPayload = {
      comparison: null,
      unavailable: reason,
      forecast: { section: null, unavailable: reason },
    };
    return NextResponse.json(payload);
  }
  if (!budgets.ok) {
    const reason = `the budget could not be read: gateway unreachable (${budgets.error})`;
    const payload: CostBudgetPayload = {
      comparison: null,
      unavailable: reason,
      forecast: { section: null, unavailable: reason },
    };
    return NextResponse.json(payload);
  }

  const payload: CostBudgetPayload = {
    comparison: budgetVsActual(series, budgets.budgets),
    unavailable: null,
    forecast: { section: burndownSection(series, budgets.budgets), unavailable: null },
  };
  return NextResponse.json(payload);
}
