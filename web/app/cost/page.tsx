// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { queryEnabledConnectors } from "@/lib/tenant";
import type { CostBudgetPayload } from "./BurndownCard";
import { CostLive, type CostPayload } from "./Live";

export default async function CostPage({
  searchParams,
}: {
  searchParams?: Promise<{ tag?: string }>;
}) {
  // Forward ?tag= to the API so the breakdown is pre-filtered to one feature (CTO-104).
  const sp = (await searchParams) ?? {};
  const query = sp.tag ? `?tag=${encodeURIComponent(sp.tag)}` : "";
  const endpoint = `/api/cost${query}`;
  // The budget comparison (CTO-209) and the burn-down forecast (CTO-210) are fetched once per
  // render rather than joining the live poll: both are tenant-wide, so the ?tag= filter does not
  // apply to them, and they change on a human/daily cadence rather than a 5-second one. A forecast
  // that flickered every 5 seconds would also read as far less trustworthy than it is.
  const [initialData, enabledLayers, budget] = await Promise.all([
    apiGet<CostPayload>(endpoint),
    queryEnabledConnectors(),
    apiGet<CostBudgetPayload>("/api/cost/budget"),
  ]);
  return (
    <CostLive
      endpoint={endpoint}
      initialData={initialData}
      enabledLayers={enabledLayers}
      budget={budget}
    />
  );
}
