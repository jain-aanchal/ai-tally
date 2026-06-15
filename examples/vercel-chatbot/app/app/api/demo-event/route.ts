// ai-tally: added file. Drives CDP events (thumbs-up, conversion) into the
// gateway. The driver POSTs here so the patched UI and the synthetic traffic
// path emit identically-shaped events.

import { type NextRequest, NextResponse } from "next/server";
import { type CdpEventType, postCdpEvent } from "@/lib/tally";

// ai-tally: see demo-chat/route.ts — Next.js 16 cacheComponents rejects
// runtime/dynamic route segment exports.

interface Body {
  sessionId: string;
  type: CdpEventType;
  valueMicroUsd?: number;
  featureTag?: string;
}

export async function POST(req: NextRequest) {
  let body: Body;
  try {
    body = (await req.json()) as Body;
  } catch {
    return NextResponse.json({ error: "bad json" }, { status: 400 });
  }
  if (!body.sessionId || !body.type) {
    return NextResponse.json(
      { error: "sessionId and type required" },
      { status: 422 },
    );
  }
  await postCdpEvent({
    sessionId: body.sessionId,
    type: body.type,
    valueMicroUsd: body.valueMicroUsd,
    featureTag: body.featureTag,
  });
  return NextResponse.json({ ok: true });
}
