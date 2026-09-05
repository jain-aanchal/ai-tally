// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import type { ForecastPayload } from "@/lib/burndown";
import { queryPriorMonthSpend } from "@/lib/clickhouse";
import { filtersToQueryString, parseFilters } from "@/lib/filters";
import { searchParamsFromRecord } from "@/lib/searchParams";
import { queryEnabledConnectors } from "@/lib/tenant";
import type { MicroUSD } from "@/lib/types";
import type { CostBudgetPayload } from "./cost/BurndownCard";
import { HomeLive, type HomePayload } from "./Live";

export default async function HomePage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  // Forward the URL-synced filter slice (CTO-226) so SSR's first paint matches the range the URL
  // asks for; the client then re-derives the same endpoint from useFilters as the range changes.
  const sp = searchParamsFromRecord(await searchParams);
  const qs = filtersToQueryString(parseFilters(sp), sp);
  const endpoint = qs ? `/api/home?${qs}` : "/api/home";
  // The month-end forecast (CTO-227) is the SAME tenant-wide projection /cost draws, read once here
  // rather than joined to the 5-second poll: it moves on a daily cadence, and a forecast that
  // flickered every few seconds would read as far less trustworthy than it is. Tenant scope only on
  // Home (no ?scope=); the full scoped burn-down stays on /cost.
  const [initialData, enabledLayers, budget, priorMonthMicroUsd] = await Promise.all([
    apiGet<HomePayload>(endpoint),
    queryEnabledConnectors(),
    apiGet<CostBudgetPayload>("/api/cost/budget"),
    // Last full month's actual, for the forecast card's "vs last month" delta (CTO-227). Read once
    // here, not polled: it only changes at a month boundary.
    queryPriorMonthSpend(),
  ]);
  const forecast: ForecastPayload = budget.forecast;
  const priorMonth: MicroUSD | null = priorMonthMicroUsd;
  return (
    <HomeLive
      initialData={initialData}
      enabledLayers={enabledLayers}
      forecast={forecast}
      priorMonthMicroUsd={priorMonth}
    />
  );
}
