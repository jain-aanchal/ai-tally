// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { mockDataQuality, mockRoi, mockSpend, sampleDataAllowed } from "@/lib/mock";
import {
  queryAttribution,
  queryDataQuality,
  queryRoi,
  querySpendSummary,
} from "@/lib/clickhouse";
import { type SourceState, readState } from "@/lib/dataState";
import { parseFilters, rangeDays } from "@/lib/filters";
import type { DataQuality, FeatureRoi, SpendSummary } from "@/lib/types";
import type { ProviderAttribution } from "@/lib/attribution";

// Read live data per request (never statically cached). A read that fails says so; it is not
// answered with fixture numbers (#364).
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface HomeSources {
  spend: SourceState;
  roi: SourceState;
  dq: SourceState;
  attribution: SourceState;
}

export interface HomeResponse {
  /** null when the source could not be read. `sources.spend` says which of the four states this is. */
  spend: SpendSummary | null;
  roi: FeatureRoi[];
  dq: DataQuality | null;
  perProviderConversion: ProviderAttribution[];
  sources: HomeSources;
}

export async function GET(request: Request) {
  // The Home time-range selector drives every headline (CTO-226): parse the URL-synced filter state
  // and resolve it to a day count the ClickHouse-derived window queries clamp and interpolate.
  const sp = new URL(request.url).searchParams;
  const state = parseFilters(sp);
  const windowDays = rangeDays(state.range);
  // Match the Attribution page's default view so the Home compact table reads
  // the same numbers a user would see on /attribution with no filters set.
  const attributionFilters = { tag: null, provider: null, outcome: "conversion" as const };
  const [spend, roi, dq, attribution] = await Promise.all([
    querySpendSummary(windowDays),
    queryRoi(windowDays),
    queryDataQuality(),
    queryAttribution(attributionFilters, { windowDays }),
  ]);

  if (sampleDataAllowed()) {
    return NextResponse.json({
      spend: mockSpend,
      roi: mockRoi,
      dq: mockDataQuality,
      perProviderConversion: attribution?.perProvider ?? [],
      sources: { spend: "sample", roi: "sample", dq: "sample", attribution: "sample" },
    } satisfies HomeResponse);
  }

  // A spend summary always comes back as an object, so "no rows" cannot be read off its shape: the
  // aggregate over an empty window is a wall of confident zeros. `spanCount` is the number the read
  // itself counted, so zero spans is the tenant having sent nothing, not a $0.00 measurement.
  return NextResponse.json({
    spend,
    roi: roi ?? [],
    dq,
    perProviderConversion: attribution?.perProvider ?? [],
    sources: {
      spend: readState(spend, (s) => (s.spanCount ?? 0) === 0),
      roi: readState(roi, (r) => r.length === 0),
      dq: readState(dq, (d) => d.attributionRate === null),
      attribution: readState(attribution, (a) => a.perProvider.length === 0),
    },
  } satisfies HomeResponse);
}
