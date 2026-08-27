// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { filtersToQueryString, parseFilters } from "@/lib/filters";
import { searchParamsFromRecord } from "@/lib/searchParams";
import { queryEnabledConnectors } from "@/lib/tenant";
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
  const [initialData, enabledLayers] = await Promise.all([
    apiGet<HomePayload>(endpoint),
    queryEnabledConnectors(),
  ]);
  return <HomeLive initialData={initialData} enabledLayers={enabledLayers} />;
}
