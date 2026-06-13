// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { getProgress, markFirstTrace } from "@/lib/onboardingStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// The live detector polls this. Returns whether the first trace has been observed yet.
export async function GET() {
  const p = getProgress();
  return NextResponse.json({
    received: p.firstTraceAt !== null,
    firstTraceAt: p.firstTraceAt,
    signedUpAt: p.signedUpAt,
  });
}

// "Send a test trace" — stands in for the customer's first proxied request arriving at ingest.
export async function POST() {
  const p = markFirstTrace();
  return NextResponse.json({ received: true, firstTraceAt: p.firstTraceAt });
}
