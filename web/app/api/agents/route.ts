// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { agents, runs } from "@/lib/agents";
import { queryAgents, queryReconcilerLastRun } from "@/lib/clickhouse";
import { parseFilters, rangeDays } from "@/lib/filters";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  // Optional URL filters (CTO-104): /api/agents?tag=aider-demo&run=<trace>. Empty values pass
  // through and the SQL clause is dropped. When a filter is set and live data is unavailable,
  // return empty rather than the unfiltered mock (which would mislead the demo).
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const { searchParams } = new URL(req.url);
  const tag = searchParams.get("tag") ?? "";
  const run = searchParams.get("run") ?? "";
  const hasFilter = Boolean(tag || run);
  // The time-range selector drives the windowed cost/day average (CTO-226): resolve the URL-synced
  // filter state to a day count the ClickHouse-derived window clamps and interpolates.
  const windowDays = rangeDays(parseFilters(searchParams).range);
  // Read agents telemetry and the reconciler's real last-run in parallel. The freshness signal is
  // the real reconciliation_runs value (CTO-169) — or null when the reconciler has never run / the
  // gateway is unavailable, which the page renders as `—` rather than a fabricated constant.
  const [live, reconcilerLastRunMinutesAgo] = await Promise.all([
    queryAgents({ tag, run }, windowDays),
    queryReconcilerLastRun(),
  ]);
  return NextResponse.json({
    agents: live && live.agents.length > 0 ? live.agents : hasFilter ? [] : agents,
    runs: live && live.runs.length > 0 ? live.runs : hasFilter ? [] : runs,
    reconcilerLastRunMinutesAgo,
  });
}
