// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { agents, RECONCILER_LAST_RUN_MINUTES_AGO, runs } from "@/lib/agents";
import { queryAgents } from "@/lib/clickhouse";

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
  const live = await queryAgents({ tag, run });
  return NextResponse.json({
    agents: live && live.agents.length > 0 ? live.agents : hasFilter ? [] : agents,
    runs: live && live.runs.length > 0 ? live.runs : hasFilter ? [] : runs,
    reconcilerLastRunMinutesAgo: RECONCILER_LAST_RUN_MINUTES_AGO,
  });
}
