// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  type AttributionDiagnostics,
  type FeatureEconomics,
  diagnostics,
  features,
} from "@/lib/features";
import {
  queryAttributionDiagnostics,
  queryFeatureEconomics,
  queryFeatureValueEvents,
} from "@/lib/clickhouse";
import { type SourceState, readState } from "@/lib/dataState";
import { type EditAccess, editAccess } from "@/lib/getTenant";
import { sampleDataAllowed } from "@/lib/mock";

// Read live data per request (never statically cached). A read that fails says so; it is not
// answered with fixture numbers (#364).
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface FeaturesResponse {
  features: FeatureEconomics[];
  /** null when the diagnostics read failed. `sources.diagnostics` says which state this is. */
  diagnostics: AttributionDiagnostics | null;
  sources: { features: SourceState; diagnostics: SourceState };
  /**
   * CTO-392: whether this caller may pin a feature's value event. POST /api/features/value-events
   * refuses a member, and the value-event CTA lives inside a client component several levels below
   * the server boundary (Cost explorer -> FeatureDetail -> FeatureValueEvents), so it rides along
   * with the data that component already fetches rather than being threaded through as a prop.
   *
   * Three states, not a boolean. Resolving the role is a gateway call that can fail, and this
   * surface is the one that shows no read-only note at all, so collapsing a failed resolve into
   * "denied" left an admin looking at "not configured" with nothing to explain it.
   */
  manageAccess: EditAccess;
}

export async function GET() {
  const [liveFeatures, liveDiagnostics, valueEvents, access] = await Promise.all([
    queryFeatureEconomics(),
    queryAttributionDiagnostics(),
    queryFeatureValueEvents(),
    editAccess(),
  ]);

  // Overlay the tenant's configured value events (CTO-140) so a just-configured feature reflects its
  // value event immediately, before attribution has produced economics for it.
  const configured = new Map((valueEvents ?? []).map((v) => [v.featureTag, v.eventName]));
  const overlay = (rows: FeatureEconomics[]) =>
    rows.map((f) => (configured.has(f.feature) ? { ...f, valueEvent: configured.get(f.feature)! } : f));

  // #364: the fixture feature roster is a demo build's. It used to stand in whenever the live read
  // came back empty, so a tenant with no features yet saw five priced features with margins as
  // their own, on Home and in the Cost explorer's feature detail alike.
  if (sampleDataAllowed()) {
    return NextResponse.json({
      features: overlay(features),
      diagnostics,
      sources: { features: "sample", diagnostics: "sample" },
      manageAccess: access,
    } satisfies FeaturesResponse);
  }

  return NextResponse.json({
    features: overlay(liveFeatures ?? []),
    // Already three-state at the source: the gateway distinguishes "no reconciler run for this
    // tenant" from "the gateway could not be read", so this route does not have to guess.
    diagnostics: liveDiagnostics.state === "live" ? liveDiagnostics.diagnostics : null,
    sources: {
      features: readState(liveFeatures, (f) => f.length === 0),
      diagnostics: liveDiagnostics.state,
    },
    manageAccess: access,
  } satisfies FeaturesResponse);
}
