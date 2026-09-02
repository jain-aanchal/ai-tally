// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "./getTenant";
// Typed client for the tenant budget control plane (CTO-208, F4).
//
// CTO-205 built the storage and the rules (`db/postgres/0026_tenant_budgets.sql`,
// `gateway/tenant_budgets.py`); until now the only way to record what a tenant intends to spend was
// to POST JSON by hand. This module is the dashboard's half of that API. Same rule as
// lib/costConnectors.ts and lib/tenant.ts: the web app never touches Postgres, every read and write
// goes through the gateway, and the gateway owns validation.
//
// TWO THINGS THIS MODULE REFUSES TO BLUR.
//
// 1. "No budget set" is not "we could not ask". The gateway answers 200 with `configured: false`
//    for a tenant who has set nothing, which is the normal state of every tenant on this system.
//    An unreachable gateway is a different fact, so `queryBudgets` reports `reachable` separately
//    and never lets a network failure render as "no budget set" (or, worse, as a budget of zero).
//
// 2. Gateway validation text reaches the user verbatim. A 409 overlap names the budget it collided
//    with, and that name is the whole value of the error: "that overlaps with research-agent-q1"
//    tells the user which row to edit, "something went wrong" sends them hunting. The
//    `conflictingBudgetId` is carried out separately as well so the UI can offer to open that row.
//
// Client-safe constants, types and the pure money helpers live in budgetsShared.ts (CTO-259) so the
// client `BudgetManager` can import them without reaching this server-only module. They are
// re-exported below so existing server call sites keep importing from "@/lib/budgets".

import {
  BUDGET_PERIODS,
  BUDGET_SCOPE_KINDS,
  type BudgetInput,
  type BudgetList,
  type BudgetWire,
  budgetFromWire,
  type DeleteResult,
  type GatewayFailure,
  type SaveResult,
} from "./budgetsShared";

export * from "./budgetsShared";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/**
 * Pull the caller-facing message out of a failed response.
 *
 * FastAPI's `detail` is a string for the 422s and an object for the 409, because the overlap error
 * carries the colliding budget_id alongside its message. Both shapes are handled here so no call
 * site has to know which endpoint returns which.
 */
async function readFailure(res: Response): Promise<GatewayFailure> {
  let detail: unknown;
  try {
    detail = ((await res.json()) as { detail?: unknown })?.detail;
  } catch {
    detail = undefined;
  }
  if (typeof detail === "string" && detail) {
    return { ok: false, error: detail, conflictingBudgetId: null };
  }
  if (detail && typeof detail === "object") {
    const d = detail as { message?: unknown; conflicting_budget_id?: unknown };
    const message = typeof d.message === "string" && d.message ? d.message : null;
    const conflicting =
      typeof d.conflicting_budget_id === "string" ? d.conflicting_budget_id : null;
    if (message) return { ok: false, error: message, conflictingBudgetId: conflicting };
  }
  return { ok: false, error: `gateway HTTP ${res.status}`, conflictingBudgetId: null };
}

/** Every budget this tenant has set. Never throws: an unreachable gateway is reported, not raised,
 *  because the page still has to render the difference between "nothing set" and "could not ask". */
export async function queryBudgets(): Promise<BudgetList> {
  const fallback: Omit<BudgetList, "reachable" | "error"> = {
    budgets: [],
    configured: false,
    periods: [...BUDGET_PERIODS],
    scopeKinds: [...BUDGET_SCOPE_KINDS],
  };
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/budgets`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    if (!res.ok) {
      const failure = await readFailure(res);
      return { ...fallback, reachable: false, error: failure.error };
    }
    const body = (await res.json()) as {
      budgets?: BudgetWire[];
      configured?: boolean;
      available_periods?: string[];
      available_scope_kinds?: string[];
    };
    return {
      budgets: (body.budgets ?? []).map(budgetFromWire),
      configured: Boolean(body.configured),
      periods: body.available_periods?.length ? body.available_periods : [...BUDGET_PERIODS],
      scopeKinds: body.available_scope_kinds?.length
        ? body.available_scope_kinds
        : [...BUDGET_SCOPE_KINDS],
      reachable: true,
      error: null,
    };
  } catch (err) {
    return { ...fallback, reachable: false, error: (err as Error).message };
  }
}

/**
 * Create or edit one budget. Creating and editing are the same call, an upsert on
 * `(tenant_id, budget_id)`, which is also why editing an existing budget in place does not trip the
 * overlap check: the gateway excludes the row from its own comparison.
 */
export async function saveBudget(input: BudgetInput): Promise<SaveResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/budgets`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify({
        budget_id: input.budgetId,
        period: input.period,
        // Integer micro-USD. The gateway rejects a float, including a whole-valued one.
        amount_micro: input.amountMicro,
        scope_kind: input.scopeKind,
        scope_value: input.scopeValue,
        starts_on: input.startsOn,
        // null, not "", not a far-future date. Open-ended is a statement.
        ends_on: input.endsOn,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return await readFailure(res);
    const body = (await res.json()) as { budget: BudgetWire };
    return { ok: true, budget: budgetFromWire(body.budget) };
  } catch (err) {
    return { ok: false, error: (err as Error).message, conflictingBudgetId: null };
  }
}

/** Withdraw one budget, returning that scope to the "no budget set" state. Idempotent: deleting an
 *  absent budget is 200 with `removed: false`, so a double-click is not an error. */
export async function deleteBudget(budgetId: string): Promise<DeleteResult> {
  try {
    const res = await fetch(
      `${GATEWAY_URL}/v1/tenant/budgets?budget_id=${encodeURIComponent(budgetId)}`,
      {
        method: "DELETE",
        headers: { "x-tenant-id": await resolveTenantId() },
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      },
    );
    if (!res.ok) return await readFailure(res);
    const body = (await res.json()) as { removed?: boolean };
    return { ok: true, removed: Boolean(body.removed) };
  } catch (err) {
    return { ok: false, error: (err as Error).message, conflictingBudgetId: null };
  }
}
