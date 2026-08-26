// SPDX-License-Identifier: Apache-2.0
// Read this tenant's budgets from the gateway (CTO-209, F5; endpoint from CTO-205, F1).
//
// Server-only, imported by Route Handlers. The dashboard never touches Postgres directly, same rule
// as `lib/tenant.ts` and `lib/allocationConfig.ts`; the gateway owns the control plane.
//
// WHY THIS DOES NOT FALL BACK TO AN EMPTY LIST. `lib/tenant.ts` degrades to `["llm"]` when the
// gateway is unreachable, and that is right for it: the worst case is a banner that stays quiet.
// Here the same move is a lie. "No budget is set" and "we could not ask" produce very different
// screens: the first is a real state with a call to action, the second must render a blank saying
// the control plane is unreachable. Returning `[]` on a timeout would tell a tenant who has a
// budget that they have none, and would hide a broken gateway behind a plausible screen. So the
// result is a discriminated union and the caller has to handle both.

import type { TenantBudget } from "./budgetVsActual";

const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";
const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** A slow control plane must not hold a page render open. Matches `lib/tenant.ts`. */
const TIMEOUT_MS = 2000;

interface TenantBudgetsResponse {
  tenant_id: string;
  budgets: TenantBudget[];
  configured: boolean;
}

export type TenantBudgetsResult =
  /** The gateway answered. `budgets` may be empty, which is a real "no budget set". */
  | { ok: true; budgets: TenantBudget[] }
  /** We could not ask. NOT the same as an empty list; the caller renders a blank, not a zero. */
  | { ok: false; error: string };

export async function fetchTenantBudgets(): Promise<TenantBudgetsResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/budgets`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) {
      // A 404 here means the gateway does not know this tenant, which is a misconfiguration and
      // emphatically not "this tenant set no budgets". Both are surfaced as an error for that
      // reason: the screen should say we could not read the budget, not invent a state.
      return { ok: false, error: `gateway HTTP ${res.status}` };
    }
    const body = (await res.json()) as TenantBudgetsResponse;
    return { ok: true, budgets: Array.isArray(body.budgets) ? body.budgets : [] };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}
