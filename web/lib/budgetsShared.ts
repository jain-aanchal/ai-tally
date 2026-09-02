// SPDX-License-Identifier: Apache-2.0
// Client-safe budget shapes and money helpers (CTO-208, F4; boundary split CTO-259).
//
// The transport half (`budgets.ts`) resolves the tenant via the server-only `getTenant`, so a Client
// Component cannot import from it without dragging the server graph in. Everything here is pure (no
// gateway fetch, no Clerk), so `BudgetManager` imports what it needs from this module while
// `budgets.ts` re-exports it for server callers.

import type { MicroUSD } from "./types";

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

export interface BudgetWire {
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

export interface GatewayFailure {
  ok: false;
  /** The gateway's own words wherever it gave us any. Never reduced to a status code. */
  error: string;
  /** Set on a 409, naming the budget the write collided with, so the UI can offer to edit it. */
  conflictingBudgetId: string | null;
}

export type SaveResult = { ok: true; budget: Budget } | GatewayFailure;
export type DeleteResult = { ok: true; removed: boolean } | GatewayFailure;

export interface BudgetInput {
  budgetId: string;
  period: string;
  amountMicro: MicroUSD;
  scopeKind: string;
  scopeValue: string;
  startsOn: string;
  endsOn: string | null;
}
