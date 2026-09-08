// SPDX-License-Identifier: Apache-2.0
// Per-layer coverage proxy (CTO-261, onboarding-agent §7). The coverage panel polls this to see
// which instrumentation layers a real span proves are flowing, and which are still dark and why.
//
// The probe itself lives in the gateway (§13): it reads otel_spans and daily_account_rollup, and
// the browser must never hold the control-plane service token, so this server-side handler is the
// seam. It resolves the active tenant from Clerk exactly as every other read does and forwards to
// the gateway's service-token-authed endpoint.
//
// A gateway that is down yields an all-unknown report with the reason attached, never a row of
// "not covered". Telling a developer their tools layer is unwired because OUR gateway blipped is
// the fabricated-negative this initiative exists to avoid (CLAUDE.md, honest under uncertainty).
import { NextResponse } from "next/server";

import { controlPlaneHeaders, getTenant } from "@/lib/getTenant";
import { type LayerCoverage, parseCoverage, unknownCoverage } from "@/lib/firstEvent";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface CoveragePayload {
  layers: LayerCoverage[];
}

// GET /api/onboarding/coverage?wired=tools,vector returns per-layer coverage for the tenant.
// `wired` passes the onboarding agent's claim about what it instrumented straight through; it only
// separates "wired, awaiting first event" from "not wired" and can never manufacture coverage.
export async function GET(req: Request): Promise<NextResponse<CoveragePayload>> {
  const wiredParam = new URL(req.url).searchParams.get("wired") ?? "";
  const wired = wiredParam
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const tenant = await getTenant();
  const qs = wiredParam ? `?wired=${encodeURIComponent(wiredParam)}` : "";
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/onboarding/coverage${qs}`, {
      headers: controlPlaneHeaders(tenant.tenantId),
      cache: "no-store",
    });
    if (!res.ok) {
      return NextResponse.json({
        layers: unknownCoverage(
          `the coverage probe answered HTTP ${res.status}, so we could not read this layer`,
        ),
      });
    }
    return NextResponse.json({ layers: parseCoverage(await res.json(), wired) });
  } catch {
    return NextResponse.json({
      layers: unknownCoverage(
        "the coverage probe could not be reached, so we could not read this layer",
      ),
    });
  }
}
