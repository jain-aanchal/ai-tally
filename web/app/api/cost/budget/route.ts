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
// The comparison is always TENANT-WIDE, even under /cost?tag=<feature>. The budget scopes in
// CTO-205 include 'feature', but wiring the tag filter to a feature-scoped budget is the
// scoped-budget phase of docs/spend-forecasting-scope.md, not this ticket, and silently comparing
// one feature's spend against the whole tenant's budget would be worse than either.

import { NextResponse } from "next/server";

import { budgetVsActual } from "@/lib/budgetVsActual";
import { querySettledCostSeries } from "@/lib/clickhouse";
import { fetchTenantBudgets } from "@/lib/tenantBudgetsClient";

import type { BudgetPayload } from "@/app/cost/BudgetVsActualCard";

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
  if (!series) {
    const payload: BudgetPayload = {
      comparison: null,
      unavailable: "spend data is unavailable: ClickHouse is unreachable from the dashboard",
    };
    return NextResponse.json(payload);
  }
  if (!budgets.ok) {
    const payload: BudgetPayload = {
      comparison: null,
      unavailable: `the budget could not be read: gateway unreachable (${budgets.error})`,
    };
    return NextResponse.json(payload);
  }

  const payload: BudgetPayload = {
    comparison: budgetVsActual(series, budgets.budgets),
    unavailable: null,
  };
  return NextResponse.json(payload);
}
