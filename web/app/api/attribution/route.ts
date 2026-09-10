// SPDX-License-Identifier: Apache-2.0
//
// #320: `perProvider` is keyed on `gen_ai.system`, and on a vector span that attribute names the
// vector vendor, so pinecone / weaviate / qdrant come back as rows here. That is faithful to what
// the SDK emits and the payload is unchanged; what changed is that the UI now calls the dimension a
// system rather than a provider. Filtering the vector rows out in this layer was the alternative
// and it was rejected: the cost column is real spend and the totals beside it are aggregated over
// the same span set, so hiding rows here would drop money from a cost-attribution view and leave
// the visible rows failing to sum to the total. Scoping the QUERY to LLM systems is a separate,
// deliberate product decision that belongs in lib/clickhouse.ts, not a presentation-layer filter.
import { NextResponse } from "next/server";

import {
  type AttributionReport,
  mockReport,
  parseFilters,
} from "@/lib/attribution";
import { queryAttribution } from "@/lib/clickhouse";
import { type SourceState, readState } from "@/lib/dataState";
import { sampleDataAllowed } from "@/lib/mock";
// The design-foundation FilterBar (CTO-221) writes range/from/to and a `feature` multi-select into
// the same query string. Reading it here (CTO-223) lets the time range and feature filter drive the
// live attribution report, while the attribution-specific tag/provider/outcome params keep working.
import { parseFilters as parseDashboardFilters, rangeDays } from "@/lib/filters";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<NextResponse> {
  const url = new URL(req.url);
  const filters = parseFilters(url.searchParams);
  const dashboard = parseDashboardFilters(url.searchParams);
  const live = await queryAttribution(filters, {
    windowDays: rangeDays(dashboard.range),
    features: dashboard.filters.feature,
  });
  // #364: the mock report used to answer BOTH "the query failed" and "the query ran and this
  // tenant has no sessions yet". The second is every tenant before its first trace, and it was
  // being shown 5,300 sessions and two providers in production as its own attribution. The demo's
  // pre-`make chatbot-demo` convenience is preserved where it belongs, behind an explicit demo
  // build with no real tenant resolved.
  if (sampleDataAllowed()) {
    const sample = mockReport(filters);
    return NextResponse.json({ ...sample, state: "sample" } satisfies AttributionResponse);
  }
  const state = readState(live, (r) => r.perProvider.length === 0);
  // An empty report is the real, correct shape for a tenant with no sessions: no providers, no
  // totals to divide, an empty daily series. It is built here rather than by nulling fields on the
  // live report so `perProvider.length === 0` stays the single thing the page tests.
  const report: AttributionReport =
    live ?? {
      filters,
      perProvider: [],
      totals: { sessions: 0, conversions: 0, costMicroUsd: 0, costPerConversionMicroUsd: null },
      dailyByProvider: [],
      isMock: false,
    };
  return NextResponse.json({ ...report, state } satisfies AttributionResponse);
}

/** The report plus which of the four states produced it. See lib/dataState.ts. */
export type AttributionResponse = AttributionReport & { state: SourceState };
