// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { filtersToQueryString, parseFilters } from "@/lib/filters";
import { searchParamsFromRecord } from "@/lib/searchParams";
import { queryEnabledConnectors } from "@/lib/tenant";
import type { CostBudgetPayload } from "./BurndownCard";
import { CostLive, type CostPayload } from "./Live";

export default async function CostPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  // Forward ?tag= (CTO-104) AND the URL-synced time range (CTO-226) to the API. The managed filter
  // serializer preserves the unmanaged tag/scope keys while adding the range, so the headline tiles
  // and the By-feature table honour the window; the interactive chart is served by /api/explore.
  const spRecord = (await searchParams) ?? {};
  const sp = searchParamsFromRecord(spRecord);
  const qs = filtersToQueryString(parseFilters(sp), sp);
  const endpoint = qs ? `/api/cost?${qs}` : "/api/cost";
  const tagValue = typeof spRecord.tag === "string" ? spRecord.tag : undefined;
  const scopeValue = typeof spRecord.scope === "string" ? spRecord.scope : undefined;
  // The budget comparison (CTO-209) and the burn-down forecast (CTO-210/211) are fetched once per
  // render rather than joining the live poll: they change on a human/daily cadence rather than a
  // 5-second one, and a forecast that flickered every 5 seconds would read as far less trustworthy
  // than it is.
  //
  // ?scope= (CTO-211) is a SEPARATE parameter from ?tag= even though both can name a feature, and
  // deliberately so. ?tag= filters the 30-day breakdown; ?scope= chooses which budget the forecast
  // is measured against, and it can also name a model or a layer. Collapsing them would mean
  // clicking a feature in the breakdown silently swapped which budget the page reports on.
  const budgetQuery = scopeValue ? `?scope=${encodeURIComponent(scopeValue)}` : "";
  const [initialData, enabledLayers, budget] = await Promise.all([
    apiGet<CostPayload>(endpoint),
    queryEnabledConnectors(),
    apiGet<CostBudgetPayload>(`/api/cost/budget${budgetQuery}`),
  ]);
  return (
    <CostLive
      initialData={initialData}
      enabledLayers={enabledLayers}
      budget={budget}
      tag={tagValue ?? null}
    />
  );
}
