import { NextResponse } from "next/server";
import { getRun } from "@/lib/agents";

export async function GET(_req: Request, ctx: { params: Promise<{ runId: string }> }) {
  const { runId } = await ctx.params;
  const run = getRun(runId);
  if (!run) return new NextResponse("Not found", { status: 404 });
  return NextResponse.json(run);
}
