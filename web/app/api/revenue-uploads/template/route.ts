// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "@/lib/getTenant";
// Serves the revenue-upload CSV template (CTO-198).
//
// A thin proxy rather than a copy of the header string: the gateway owns what the columns are, and
// a template that drifts from the parser is worse than no template. If the gateway is unreachable
// we say so instead of handing over a guessed header that might be rejected on upload.
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export async function GET() {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/revenue-uploads/template`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) throw new Error(`gateway HTTP ${res.status}`);
    return new NextResponse(await res.text(), {
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": 'attachment; filename="revenue-template.csv"',
      },
    });
  } catch (err) {
    return NextResponse.json(
      { error: `template unavailable: ${(err as Error).message}` },
      { status: 503 },
    );
  }
}
