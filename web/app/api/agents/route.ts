import { NextResponse } from "next/server";

import { agents, runs } from "@/lib/agents";
import { queryAgents } from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const live = await queryAgents();
  return NextResponse.json({
    agents: live && live.agents.length > 0 ? live.agents : agents,
    runs: live && live.runs.length > 0 ? live.runs : runs,
  });
}
