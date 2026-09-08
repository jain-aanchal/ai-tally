// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { FUNNEL_STAGES, type FunnelStage } from "@/lib/onboarding";
import { getCreds, getFunnel, getProgress, hasFunnelStage, recordFunnel } from "@/lib/onboardingStore";

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
//
// #329: `noticed` says the client observed the stage rather than performed it. The onboarding page
// reports first_trace that way, off the coverage probe, because the probe proves a trace arrived
// and not when. A noticed stage is recorded once and never stamps a progress timestamp, so nothing
// downstream can read the moment we spotted it as the moment it happened.
export async function POST(req: Request) {
  let body: { stage?: string; noticed?: boolean };
  try {
    body = (await req.json()) as { stage?: string; noticed?: boolean };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const stage = body.stage as FunnelStage | undefined;
  if (!stage || !FUNNEL_STAGES.includes(stage)) {
    return NextResponse.json({ error: "unknown funnel stage" }, { status: 400 });
  }
  const noticed = body.noticed === true;
  // A polled observation repeats on every page load, so only the first report is kept.
  if (noticed && hasFunnelStage(stage)) {
    return NextResponse.json({ event: null, progress: getProgress() });
  }
  const event = recordFunnel(stage, { noticed });
  return NextResponse.json({ event, progress: getProgress() });
}
