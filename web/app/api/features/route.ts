import { NextResponse } from "next/server";

import { diagnostics, features } from "@/lib/features";
import { queryAttributionDiagnostics, queryFeatureEconomics } from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [liveFeatures, liveDiagnostics] = await Promise.all([
    queryFeatureEconomics(),
    queryAttributionDiagnostics(),
  ]);
  return NextResponse.json({
    features: liveFeatures && liveFeatures.length > 0 ? liveFeatures : features,
    diagnostics: liveDiagnostics ?? diagnostics,
  });
}
