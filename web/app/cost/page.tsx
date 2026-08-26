// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { queryEnabledConnectors } from "@/lib/tenant";
import type { CostBudgetPayload } from "./BurndownCard";
import { CostLive, type CostPayload } from "./Live";

export default async function CostPage({
  searchParams,
}: {
  searchParams?: Promise<{ tag?: string; scope?: string }>;
}) {
  // Forward ?tag= to the API so the breakdown is pre-filtered to one feature (CTO-104).
  const sp = (await searchParams) ?? {};
  const query = sp.tag ? `?tag=${encodeURIComponent(sp.tag)}` : "";
  const endpoint = `/api/cost${query}`;
  // The budget comparison (CTO-209) and the burn-down forecast (CTO-210/211) are fetched once per
  // render rather than joining the live poll: they change on a human/daily cadence rather than a
  // 5-second one, and a forecast that flickered every 5 seconds would read as far less trustworthy
  // than it is.
  //
  // ?scope= (CTO-211) is a SEPARATE parameter from ?tag= even though both can name a feature, and
  // deliberately so. ?tag= filters the 30-day breakdown; ?scope= chooses which budget the forecast
  // is measured against, and it can also name a model or a layer. Collapsing them would mean
  // clicking a feature in the breakdown silently swapped which budget the page reports on.
  const budgetQuery = sp.scope ? `?scope=${encodeURIComponent(sp.scope)}` : "";
  const [initialData, enabledLayers, budget] = await Promise.all([
    apiGet<CostPayload>(endpoint),
    queryEnabledConnectors(),
    apiGet<CostBudgetPayload>(`/api/cost/budget${budgetQuery}`),
  ]);
  return (
    <CostLive
      endpoint={endpoint}
      initialData={initialData}
      enabledLayers={enabledLayers}
      budget={budget}
      tag={sp.tag ?? null}
    />
  );
}
