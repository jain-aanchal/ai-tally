// SPDX-License-Identifier: Apache-2.0
// Workflow 4 — business-outcome attribution.
//
// Reads ?tag=&provider=&outcome= and renders $/conversion per provider with
// Wilson 95% intervals on conversion rate. The chatbot demo's run.sh deep-links
// here with ?tag=chatbot-demo&outcome=positive_feedback.
//
// CTO-223: the page now also honours the design-foundation FilterBar's query (range/from/to and a
// `feature` multi-select). Those params ride in the same endpoint string the client polls, so the
// time range and feature filter drive the live report exactly as tag/provider/outcome always have.
// The endpoint is built by preserving EVERY incoming param (so `?tag=`/`?scope=` and the FilterBar
// keys all survive) and only defaulting `outcome`.

import { apiGet } from "@/lib/api";
import type { AttributionReport } from "@/lib/attribution";
import { querySpanFeatureTags } from "@/lib/clickhouse";
import { parseFilters, rangeDays } from "@/lib/filters";
import { AttributionLive } from "./Live";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function readStr(
  params: Record<string, string | string[] | undefined>,
  key: string,
): string | null {
  const v = params[key];
  if (Array.isArray(v)) return v[0] ?? null;
  return v ?? null;
}

export default async function AttributionPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const outcome = readStr(params, "outcome") ?? "conversion";

  // Preserve every incoming param in the endpoint the client polls, so the FilterBar's window and
  // feature filter (and tag/scope) all reach /api/attribution unchanged. Only `outcome` is defaulted.
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (typeof v === "string") qs.set(k, v);
    else if (Array.isArray(v) && v[0] !== undefined) qs.set(k, v[0]);
  }
  qs.set("outcome", outcome);

  const filterState = parseFilters(qs);
  const windowDays = rangeDays(filterState.range);

  const endpoint = `/api/attribution?${qs.toString()}`;
  const [initialData, featureTags] = await Promise.all([
    apiGet<AttributionReport>(endpoint),
    querySpanFeatureTags(windowDays),
  ]);

  return (
    <AttributionLive
      endpoint={endpoint}
      initialData={initialData}
      outcome={outcome}
      featureTags={featureTags ?? []}
    />
  );
}
