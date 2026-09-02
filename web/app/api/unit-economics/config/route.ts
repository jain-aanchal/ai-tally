// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "@/lib/getTenant";
import { NextResponse } from "next/server";

import {
  DEFAULT_THRESHOLDS,
  resolveThresholds,
  type UnitEconomicsThresholds,
} from "@/lib/unitEconomics";
import { queryUnitEconomicsConfig } from "@/lib/unitEconomicsConfig";

// Per-tenant LTV/CAC band thresholds live in the control plane (Postgres, CTO-126), reached via the
// gateway. The reader falls back to `null` (→ hardcoded defaults) when the gateway is unreachable, so
// `npm run dev/build/test` never depend on infra.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export interface ThresholdConfigPayload {
  /** The tenant's resolved thresholds (overrides layered on defaults, or defaults when no row). */
  thresholds: UnitEconomicsThresholds;
  /** The hardcoded defaults — the settings panel's reset target. */
  defaults: UnitEconomicsThresholds;
  /** True when the tenant has a stored override row (vs. running on pure defaults). */
  hasOverride: boolean;
}

// GET /api/unit-economics/config — the tenant's resolved thresholds + defaults for the settings panel.
export async function GET(): Promise<NextResponse<ThresholdConfigPayload>> {
  const overrides = await queryUnitEconomicsConfig();
  return NextResponse.json({
    thresholds: resolveThresholds(overrides),
    defaults: DEFAULT_THRESHOLDS,
    hasOverride: overrides !== null,
  });
}

// POST /api/unit-economics/config — persist edited thresholds. Validates the shape, then forwards to
// the gateway's idempotent upsert with a client-supplied change_id (UUID). When the gateway is
// unreachable we validate and echo the values back (persisted:false) so the prototype works without
// infra.
export async function POST(req: Request) {
  let body: Partial<UnitEconomicsThresholds> & { updatedBy?: string };
  try {
    body = (await req.json()) as Partial<UnitEconomicsThresholds> & { updatedBy?: string };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }

  const fields: (keyof UnitEconomicsThresholds)[] = [
    "ltvCacGreen",
    "ltvCacYellow",
    "paybackGreen",
    "paybackYellow",
  ];
  for (const f of fields) {
    const v = body[f];
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0) {
      return NextResponse.json({ error: `${f} must be a number >= 0` }, { status: 422 });
    }
  }
  const t = body as UnitEconomicsThresholds;
  if (t.ltvCacGreen < t.ltvCacYellow) {
    return NextResponse.json(
      { error: "ltvCacGreen must be >= ltvCacYellow" },
      { status: 422 },
    );
  }
  if (t.paybackGreen > t.paybackYellow) {
    return NextResponse.json(
      { error: "paybackGreen must be <= paybackYellow" },
      { status: 422 },
    );
  }

  const changeId = crypto.randomUUID();
  const payload = {
    ltv_cac_green_threshold: t.ltvCacGreen,
    ltv_cac_yellow_threshold: t.ltvCacYellow,
    payback_months_green: t.paybackGreen,
    payback_months_yellow: t.paybackYellow,
    change_id: changeId,
    updated_by: body.updatedBy ?? null,
  };

  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/unit-economics/config`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) {
      return NextResponse.json({ thresholds: t, changeId, persisted: true });
    }
    if (res.status >= 400 && res.status < 500) {
      const detail = await res.text();
      return NextResponse.json(
        { error: `gateway rejected upsert: ${detail}` },
        { status: 422 },
      );
    }
    return NextResponse.json({ error: `gateway error ${res.status}` }, { status: 502 });
  } catch {
    // Gateway unreachable (CI / fresh clone): echo the validated thresholds so the prototype works.
    return NextResponse.json({ thresholds: t, changeId, persisted: false });
  }
}
