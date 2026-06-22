// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  CONFIG_REFRESH_SECONDS,
  type GuardrailMode,
  type GuardrailRule,
  guardrailRules,
} from "@/lib/guardrails";
import { queryGuardrailRules } from "@/lib/clickhouse";

// Guardrail config lives in the control plane (Postgres, CTO-27/116), reached via the gateway. The
// reader (queryGuardrailRules) falls back to the typed mock when the gateway is unreachable, so
// `npm run dev/build/test` never depend on infra.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";
const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";

// Map the web mode back onto the control-plane state for the upsert. Only `enabled` actually alters
// the agent; observe maps to `shadow` (recording-only). The precise mode is preserved in params.mode
// so the reader round-trips it.
function stateForMode(mode: GuardrailMode): "enabled" | "shadow" | "disabled" {
  return mode === "observe" ? "shadow" : "enabled";
}

// Map a web rule's caps onto a control-plane kind: cost_cap when a cost cap is set, else loop_limit
// for a step cap. (pii_gate / model_deprecation are managed elsewhere — out of scope for CTO-120.)
function kindForRule(
  rule: Pick<GuardrailRule, "maxCostMicroUsd" | "maxSteps">,
): "cost_cap" | "loop_limit" {
  return rule.maxCostMicroUsd != null ? "cost_cap" : "loop_limit";
}

// GET /api/guardrails           -> rules (live via gateway, falling back to the mock) + refresh window
// GET /api/guardrails?audit=1   -> recent guardrail rule changes for the tenant (optionally ?rule_id=)
export async function GET(req: Request) {
  const url = new URL(req.url);
  if (url.searchParams.get("audit") === "1") {
    const ruleId = url.searchParams.get("rule_id") ?? undefined;
    try {
      const qs = ruleId ? `?rule_id=${encodeURIComponent(ruleId)}` : "";
      const res = await fetch(`${GATEWAY_URL}/v1/tenant/guardrails/audit${qs}`, {
        headers: { "x-tenant-id": TENANT },
        cache: "no-store",
        signal: AbortSignal.timeout(2000),
      });
      if (!res.ok) {
        return NextResponse.json({ changes: [], unavailable: true });
      }
      const body = (await res.json()) as { changes?: unknown[] };
      return NextResponse.json({ changes: Array.isArray(body.changes) ? body.changes : [] });
    } catch {
      // Gateway unreachable (CI / fresh clone): empty audit, flagged so the UI can say "unavailable".
      return NextResponse.json({ changes: [], unavailable: true });
    }
  }

  const rules = (await queryGuardrailRules()) ?? guardrailRules;
  return NextResponse.json({ rules, configRefreshSeconds: CONFIG_REFRESH_SECONDS });
}

// POST /api/guardrails — persist an edited rule. Validates the shape, then forwards to the gateway's
// idempotent upsert with a client-supplied change_id (UUID). When the gateway is unreachable we still
// validate and echo the rule back (the client treats the echo as the saved state) so the prototype
// works without infra; the SDK picks the change up on its next config-refresh window.
export async function POST(req: Request) {
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

  const changeId = crypto.randomUUID();
  const maxCostMicroUsd = rule.maxCostMicroUsd ?? null;
  const maxSteps = rule.maxSteps ?? null;
  const payload = {
    rule_id: rule.id,
    kind: kindForRule({ maxCostMicroUsd, maxSteps }),
    state: stateForMode(rule.mode),
    change_id: changeId,
    params: {
      mode: rule.mode,
      scope: rule.scope,
      scope_kind: rule.scopeKind ?? "agent",
      max_cost_micro_usd: maxCostMicroUsd,
      max_steps: maxSteps,
    },
  };

  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/guardrails`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": TENANT },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) {
      const body = (await res.json()) as { rule?: unknown };
      return NextResponse.json({
        rule,
        gatewayRule: body.rule ?? null,
        changeId,
        configRefreshSeconds: CONFIG_REFRESH_SECONDS,
      });
    }
    // Gateway rejected the upsert (e.g. validation) — surface 422 so the client knows it didn't persist.
    if (res.status >= 400 && res.status < 500) {
      const detail = await res.text();
      return NextResponse.json({ error: `gateway rejected upsert: ${detail}` }, { status: 422 });
    }
    return NextResponse.json({ error: `gateway error ${res.status}` }, { status: 502 });
  } catch {
    // Gateway unreachable (CI / fresh clone): echo the validated rule so the prototype still works.
    return NextResponse.json({
      rule,
      changeId,
      persisted: false,
      configRefreshSeconds: CONFIG_REFRESH_SECONDS,
    });
  }
}
