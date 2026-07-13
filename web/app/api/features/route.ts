// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { diagnostics, features } from "@/lib/features";
import {
  queryAttributionDiagnostics,
  queryFeatureEconomics,
  queryFeatureValueEvents,
} from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [liveFeatures, liveDiagnostics, valueEvents] = await Promise.all([
    queryFeatureEconomics(),
    queryAttributionDiagnostics(),
    queryFeatureValueEvents(),
  ]);
  const base = liveFeatures && liveFeatures.length > 0 ? liveFeatures : features;
  // Overlay the tenant's configured value events (CTO-140) so a just-configured feature reflects its
  // value event immediately, before attribution has produced economics for it.
  const configured = new Map((valueEvents ?? []).map((v) => [v.featureTag, v.eventName]));
  const merged = base.map((f) =>
    configured.has(f.feature) ? { ...f, valueEvent: configured.get(f.feature)! } : f,
  );
  return NextResponse.json({
    features: merged,
    diagnostics: liveDiagnostics ?? diagnostics,
  });
}
