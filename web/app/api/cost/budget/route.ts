// SPDX-License-Identifier: Apache-2.0
// Month-to-date actual versus budget for /cost (CTO-209, F5), the burn-down forecast (CTO-210, F6)
// and the per-scope roster (CTO-211, F7).
//
// WHY THIS IS ITS OWN ROUTE AND NOT PART OF /api/cost. The cost page live-polls /api/cost every 5
// seconds (`useLivePoll`, NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS). This payload costs several
// ClickHouse reads plus a gateway round trip, and none of it changes at that cadence: a budget is
// edited by a human maybe monthly, and the settled window advances once a day. Folding it into the
// polled route would multiply that work by 720 an hour to redraw an identical section. So it is
// fetched once per page render, server side, and the section states the days it covers instead of
// pretending to be live.
//
// CTO-210 (F6) adds the burn-down section to the same payload rather than a second route. Both
// sections are derived from ONE `querySettledCostSeries` result, so the measured month-to-date
// figure and the projection can never be reading two different windows.
//
// CTO-211 (F7) makes the forecast half scoped. Three things follow, in this order, and the order is
// forced:
//
//  1. The budgets have to be read BEFORE the spend, because the budgets are what decide which
//     scopes exist. `rosterScopes` derives the roster from the monthly budgets covering today, so a
//     tenant with 200 feature tags gets a selector of the two or three somebody actually owns
//     rather than 200 ClickHouse reads for scopes with nothing to be on track against.
//
//  2. Each scope gets its OWN `querySettledCostSeries(scope)` (CTO-207 has taken a scope argument
//     since F3). Filtering a tenant-wide series afterwards would be cheaper and wrong: the
//     leading-zero trim and the fourteen-day floor are counted in the scope's own settled days, and
//     a feature introduced last week has to fail that floor on a tenant that has been running for a
//     year. Reading per scope is what makes the guard per scope.
//
//  3. The measured card above stays TENANT-WIDE whatever the selector says. Rescoping it too would
//     mean the same page had two different definitions of "month to date" one card apart, and the
//     scoped figure is already on screen: it is in the roster table and in the forecast section's
//     own settled line, both labelled with the scope they belong to.
//
// A `?scope=` naming something with no budget is not honoured silently: the payload falls back to
// tenant-wide and carries `selectionFallbackReason`, which the card prints.

import { NextResponse } from "next/server";

import { budgetVsActual } from "@/lib/budgetVsActual";
import { burndownSection } from "@/lib/burndown";
import { querySettledCostSeries, type SpendScope } from "@/lib/clickhouse";
import { LAYERS, type Layer } from "@/lib/cost";
import {
  rosterScopes,
  scopedForecast,
  type ScopeSection,
  type ScopedForecastPayload,
} from "@/lib/scopedForecast";
import { parseScopeKey, type ForecastScope } from "@/lib/spendScopes";
import { fetchTenantBudgets } from "@/lib/tenantBudgetsClient";

import type { CostBudgetPayload } from "@/app/cost/BurndownCard";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * A roster scope as `querySettledCostSeries` wants it, or null when this deployment cannot query it.
 *
 * The only way to get a null here is a `scope_kind='layer'` budget naming a layer this build does
 * not have, which the gateway does not police. Dropping it from the roster is better than querying
 * a layer name that matches nothing and rendering a confident zero for it.
 */
function toSpendScope(scope: ForecastScope): SpendScope | null {
  switch (scope.kind) {
    case "tenant":
      return { kind: "tenant" };
    case "feature":
      return { kind: "feature", value: scope.value };
    case "model":
      return { kind: "model", value: scope.value };
    case "layer":
      return (LAYERS as readonly string[]).includes(scope.value)
        ? { kind: "layer", value: scope.value as Layer }
        : null;
  }
}

export async function GET(request: Request) {
  const requestedKey = new URL(request.url).searchParams.get("scope");
  const requested = parseScopeKey(requestedKey);
  const requestedReason =
    requestedKey && !requested
      ? `"${requestedKey}" does not name a scope this dashboard can forecast, so the tenant-wide ` +
        "forecast is shown instead"
      : null;

  // Budgets first: they decide the roster (note 1 above). The tenant-wide series is read alongside
  // them because it is needed in every branch, including the fallback.
  const [tenantSeries, budgets] = await Promise.all([
    querySettledCostSeries({ kind: "tenant" }),
    fetchTenantBudgets(),
  ]);

  // No mock fallback on this surface, unlike the rest of /cost. Everywhere else a canned series
  // keeps a fresh clone looking alive; here it would put invented spend next to a budget somebody
  // really set and produce a variance that is pure fiction. "We cannot read the spend" is the only
  // honest answer, and the section renders it as a blank with this reason attached.
  // The same reason blanks every section: a projection built on invented spend and compared against
  // a budget somebody really set would be fiction with a date attached, which is worse here than on
  // the measured card because a reader would act on it days in advance.
  if (!tenantSeries) {
    return NextResponse.json(unavailable("spend data is unavailable: ClickHouse is unreachable from the dashboard"));
  }
  if (!budgets.ok) {
    return NextResponse.json(
      unavailable(`the budget could not be read: gateway unreachable (${budgets.error})`),
    );
  }

  // Today per ClickHouse, never the Node clock: the roster's "covering today" test has to agree
  // with the one `selectBudget` applies inside each section (CTO-203).
  const roster = rosterScopes(budgets.budgets, tenantSeries.windowEnd);

  const sections: ScopeSection[] = [
    { scope: { kind: "tenant", value: "" }, section: burndownSection(tenantSeries, budgets.budgets) },
  ];
  const scoped = roster.filter((s) => s.kind !== "tenant");
  const scopedSeries = await Promise.all(
    scoped.map(async (scope) => {
      const spendScope = toSpendScope(scope);
      // Each scope reads its own series (note 2). A scope whose read fails is dropped from the
      // roster rather than shown with tenant numbers under its name.
      const series = spendScope ? await querySettledCostSeries(spendScope) : null;
      return { scope, series };
    }),
  );
  for (const { scope, series } of scopedSeries) {
    if (!series) continue;
    sections.push({ scope, section: burndownSection(series, budgets.budgets, scope) });
  }

  const result = scopedForecast({
    sections,
    requested,
    requestedReason,
    budgets: budgets.budgets,
  });

  const payload: CostBudgetPayload = {
    // Tenant-wide whatever the selector says (note 3).
    comparison: budgetVsActual(tenantSeries, budgets.budgets),
    unavailable: null,
    // The selected scope's section, so `BurndownCard` renders one section and never has to decide
    // between two sources for it. With no `?scope=` this is the tenant-wide section, byte for byte
    // what CTO-210 shipped.
    forecast: { section: result.section, unavailable: null },
    scoped: { scoped: result, unavailable: null },
  };
  return NextResponse.json(payload);
}

function unavailable(reason: string): CostBudgetPayload {
  const scoped: ScopedForecastPayload = { scoped: null, unavailable: reason };
  return {
    comparison: null,
    unavailable: reason,
    forecast: { section: null, unavailable: reason },
    scoped,
  };
}
