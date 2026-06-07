// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { FUNNEL_STAGES, type FunnelStage } from "@/lib/onboarding";
import { getCreds, getFunnel, getProgress, recordFunnel } from "@/lib/onboardingStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json({
    progress: getProgress(),
    creds: getCreds(),
    funnel: getFunnel(),
  });
}

// Record an activation-funnel event (e.g. the client reports the config was copied).
export async function POST(req: Request) {
  let body: { stage?: string };
  try {
    body = (await req.json()) as { stage?: string };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const stage = body.stage as FunnelStage | undefined;
  if (!stage || !FUNNEL_STAGES.includes(stage)) {
    return NextResponse.json({ error: "unknown funnel stage" }, { status: 400 });
  }
  const event = recordFunnel(stage);
  return NextResponse.json({ event, progress: getProgress() });
}
