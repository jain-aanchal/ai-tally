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

import type { MicroUSD } from "./types";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** Mirrors gateway.tenant_budgets.BUDGET_PERIODS. The gateway echoes its own list; this is the
 *  fallback used when it is unreachable, so the form still renders something truthful. */
export const BUDGET_PERIODS = ["month", "quarter"] as const;
export type BudgetPeriod = (typeof BUDGET_PERIODS)[number];

/** Mirrors gateway.tenant_budgets.BUDGET_SCOPE_KINDS. */
export const BUDGET_SCOPE_KINDS = ["tenant", "feature", "model", "layer"] as const;
export type BudgetScopeKind = (typeof BUDGET_SCOPE_KINDS)[number];

/** The scope kind that covers the whole bill and therefore names nothing. */
export const TENANT_SCOPE_KIND: BudgetScopeKind = "tenant";

export interface Budget {
  budgetId: string;
  period: string;
  amountMicro: MicroUSD;
  scopeKind: string;
  /** Always a string, '' for a tenant-wide budget. Never null: see the gateway's as_dict(). */
  scopeValue: string;
  /** ISO YYYY-MM-DD. */
  startsOn: string;
  /** null means open-ended ("until further notice"), not unknown and not a sentinel date. */
  endsOn: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

interface BudgetWire {
  budget_id: string;
  period: string;
  amount_micro: number;
  scope_kind: string;
  scope_value: string;
  starts_on: string;
  ends_on: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface BudgetList {
  budgets: Budget[];
  /** false when the tenant has set no budget at all. A normal state, not an error. */
  configured: boolean;
  /** What this deployment can store, echoed by the gateway so the form cannot drift from the
   *  CHECK constraints. Falls back to the constants above when the gateway is unreachable. */
  periods: string[];
  scopeKinds: string[];
  /** false when we could not reach the gateway. Deliberately distinct from `configured`: a page
   *  must not tell a user "no budget set" when the truth is "we could not ask". */
  reachable: boolean;
  /** Verbatim failure text when `reachable` is false. */
  error: string | null;
}

export function budgetFromWire(w: BudgetWire): Budget {
  return {
    budgetId: w.budget_id,
    period: w.period,
    amountMicro: w.amount_micro,
    scopeKind: w.scope_kind,
    scopeValue: w.scope_value ?? "",
    startsOn: w.starts_on,
    endsOn: w.ends_on,
    createdAt: w.created_at,
    updatedAt: w.updated_at,
  };
}

/** How a budget reads in a table cell: "Whole tenant", or "feature: research-agent". */
export function scopeLabel(budget: Pick<Budget, "scopeKind" | "scopeValue">): string {
  if (budget.scopeKind === TENANT_SCOPE_KIND) return "Whole tenant";
  return `${budget.scopeKind}: ${budget.scopeValue}`;
}

// ---------------------------------------------------------------------------------------------
// Money. Dollars in the form, integer micro-USD on the wire, and no float in between.
// ---------------------------------------------------------------------------------------------

/** Micro-USD per dollar. The gateway rejects a float amount outright, so the conversion has to be
 *  exact, not merely close. */
const MICRO_PER_USD = 1_000_000;

export type DollarParse = { ok: true; micro: MicroUSD } | { ok: false; error: string };

/**
 * Parse a dollars-and-cents string into integer micro-USD, by STRING SURGERY rather than
 * arithmetic on a float.
 *
 * `Math.round(Number("30000.07") * 1e6)` looks fine and is fine for most inputs, which is exactly
 * why it is dangerous: it fails on a small, unpredictable subset (0.1 + 0.2 territory) and a budget
 * that is one micro-dollar off is a variance figure that never quite reconciles. Splitting on the
 * decimal point and padding the fraction to six digits is exact for every input the form accepts.
 *
 * Zero is accepted. A budget of zero is a real claim ("this scope may spend nothing"), and it is
 * the ABSENCE of a budget, not a zero, that means "no budget set".
 */
export function dollarsToMicro(input: string): DollarParse {
  // Commas and a leading $ are what people paste out of a spreadsheet. Accepting them is not
  // guessing at intent, it is reading the same number.
  const cleaned = input.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!cleaned) return { ok: false, error: "amount is required, in dollars (for example 30000)" };
  if (!/^\d+(\.\d{1,6})?$/.test(cleaned)) {
    return {
      ok: false,
      error:
        "amount must be a positive dollar figure with at most 6 decimal places " +
        "(for example 30000 or 1250.50)",
    };
  }
  const [whole, fraction = ""] = cleaned.split(".");
  const micro = Number(whole) * MICRO_PER_USD + Number(fraction.padEnd(6, "0"));
  if (!Number.isSafeInteger(micro)) {
    return { ok: false, error: "amount is too large to record" };
  }
  return { ok: true, micro };
}

/**
 * Micro-USD back to the exact dollar string the edit form should start from.
 *
 * Not `formatUSD`: that one is for display and rounds to a readable number of decimals, so feeding
 * it back into an input would let an edit-and-save silently change an amount it never showed the
 * user. This is the round-trip inverse of `dollarsToMicro` and nothing else.
 */
export function microToDollarInput(micro: MicroUSD): string {
  const negative = micro < 0;
  const abs = Math.abs(Math.trunc(micro));
  const whole = Math.floor(abs / MICRO_PER_USD);
  const fraction = String(abs % MICRO_PER_USD).padStart(6, "0").replace(/0+$/, "");
  const body = fraction ? `${whole}.${fraction}` : String(whole);
  return negative ? `-${body}` : body;
}

// ---------------------------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------------------------

export interface GatewayFailure {
  ok: false;
  /** The gateway's own words wherever it gave us any. Never reduced to a status code. */
  error: string;
  /** Set on a 409, naming the budget the write collided with, so the UI can offer to edit it. */
  conflictingBudgetId: string | null;
}

export type SaveResult = { ok: true; budget: Budget } | GatewayFailure;
export type DeleteResult = { ok: true; removed: boolean } | GatewayFailure;

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

export interface BudgetInput {
  budgetId: string;
  period: string;
  amountMicro: MicroUSD;
  scopeKind: string;
  scopeValue: string;
  startsOn: string;
  endsOn: string | null;
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
