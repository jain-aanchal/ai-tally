// SPDX-License-Identifier: Apache-2.0
// First-data onboarding probe endpoint (Initiative 2, §9). The onboarding panel polls this to flip
// from "waiting for your first event" to "connected". It runs a tenant-scoped existence check
// against ClickHouse (SELECT 1 FROM otel_spans WHERE TenantId = {tenant} LIMIT 1) using the canonical
// tenant UUID from getTenant(), and returns the honest state only: connected / waiting / unknown.
//
// Server-only (nodejs runtime): it reaches ClickHouse through the same lib the dashboard reads use,
// and the tenant resolves from Clerk (or the dev escape hatch) exactly as every other read does.
import { NextResponse } from "next/server";

import { queryFirstEventSeen } from "@/lib/clickhouse";
import type { FirstEventStatus } from "@/lib/firstEvent";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface FirstEventPayload {
  status: FirstEventStatus;
}

// GET /api/onboarding/first-event — has any span landed for the caller's tenant yet?
export async function GET(): Promise<NextResponse<FirstEventPayload>> {
  const status = await queryFirstEventSeen();
  return NextResponse.json({ status });
}
