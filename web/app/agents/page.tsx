// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { filtersToQueryString, parseFilters } from "@/lib/filters";
import { searchParamsFromRecord } from "@/lib/searchParams";
import { AgentsLive, type AgentsPayload } from "./Live";

export default async function AgentsPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  // Forward ?tag= / ?run= (CTO-104 deep links) AND the URL-synced time range (CTO-226): the managed
  // filter serializer preserves the unmanaged tag/run keys while adding the range, so SSR's first
  // paint matches the URL and the client re-derives the same endpoint from useFilters as it changes.
  const sp = searchParamsFromRecord(await searchParams);
  const qs = filtersToQueryString(parseFilters(sp), sp);
  const endpoint = qs ? `/api/agents?${qs}` : "/api/agents";
  const initialData = await apiGet<AgentsPayload>(endpoint);
  return <AgentsLive initialData={initialData} />;
}
