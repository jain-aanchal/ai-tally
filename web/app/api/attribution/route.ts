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
  // Fall back to the mock report when the query failed (null) OR when there's
  // no chatbot-demo data yet (live but empty). Mirrors the pattern used by
  // /api/agents and /api/cost — and keeps the demo's attribution view useful
  // before the user runs `make chatbot-demo` for the first time.
  const report: AttributionReport =
    live && live.perProvider.length > 0 ? live : mockReport(filters);
  return NextResponse.json(report);
}
