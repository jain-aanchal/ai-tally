import { NextResponse } from "next/server";

import { getRun } from "@/lib/agents";
import { queryAgentRun } from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(_req: Request, ctx: { params: Promise<{ runId: string }> }) {
  const { runId } = await ctx.params;
  const run = (await queryAgentRun(runId)) ?? getRun(runId);
  if (!run) return new NextResponse("Not found", { status: 404 });
  return NextResponse.json(run);
}
