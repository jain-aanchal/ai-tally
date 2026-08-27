// SPDX-License-Identifier: Apache-2.0
// The /waste dashboard page (CTO-235, W8 of epic CTO-227). Server component: it forwards the same
// URL-synced filter state the FilterBar writes (range / from / to / feature / model / provider /
// layer / account) to /api/waste, so SSR's first paint and the client's re-query read the one string.
// The endpoint runs every detector and rolls the findings up; this page only renders the report.

import { apiGet } from "@/lib/api";
import { filtersToQueryString, parseFilters } from "@/lib/filters";
import { searchParamsFromRecord } from "@/lib/searchParams";
import type { WasteReport } from "@/lib/waste";
import { WasteLive } from "./Live";

export default async function WastePage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  // Mirror agents/page.tsx: the managed serializer rebuilds the query from the incoming params, so any
  // deep-link key is preserved while the window + dimension filters ride onto /api/waste. The client
  // re-derives the same endpoint from useFilters as the filters change, keeping SSR and CSR in step.
  const sp = searchParamsFromRecord(await searchParams);
  const qs = filtersToQueryString(parseFilters(sp), sp);
  const endpoint = qs ? `/api/waste?${qs}` : "/api/waste";
  const initialData = await apiGet<WasteReport>(endpoint);
  return <WasteLive initialData={initialData} />;
}
