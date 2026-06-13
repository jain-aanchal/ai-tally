// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  CONFIG_REFRESH_SECONDS,
  type GuardrailRule,
  guardrailRules,
} from "@/lib/guardrails";

// Guardrail config lives in the control plane (Postgres, CTO-27), not ClickHouse. No live reader is
// wired here yet, so we serve the typed mock directly — `npm run dev/build/test` never need infra.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json({
    rules: guardrailRules,
    configRefreshSeconds: CONFIG_REFRESH_SECONDS,
  });
}

// Persisting an edited rule. With no control plane wired in the prototype this validates the shape
// and echoes the rule back (the client treats the echo as the saved state). The SDK would pick the
// change up on its next config refresh.
export async function PUT(req: Request) {
  let rule: Partial<GuardrailRule>;
  try {
    rule = (await req.json()) as Partial<GuardrailRule>;
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!rule.id || !rule.scope || !rule.mode) {
    return NextResponse.json({ error: "id, scope and mode are required" }, { status: 400 });
  }
  if (rule.maxCostMicroUsd == null && rule.maxSteps == null) {
    return NextResponse.json(
      { error: "a rule must set a cost cap or a step cap" },
      { status: 422 },
    );
  }
  return NextResponse.json({ rule, configRefreshSeconds: CONFIG_REFRESH_SECONDS });
}
