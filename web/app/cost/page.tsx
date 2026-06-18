// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { queryEnabledConnectors } from "@/lib/tenant";
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
  const [initialData, enabledLayers] = await Promise.all([
    apiGet<CostPayload>(endpoint),
    queryEnabledConnectors(),
  ]);
  return (
    <CostLive endpoint={endpoint} initialData={initialData} enabledLayers={enabledLayers} />
  );
}
