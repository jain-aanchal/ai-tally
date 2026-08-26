// SPDX-License-Identifier: Apache-2.0
"use server";

// Server actions behind the budget settings form (CTO-208, F4).
//
// Same shape as app/connectors/costConnectorActions.ts: the browser never talks to the gateway and
// never sees the tenant header, and field validation belongs to the gateway rather than being
// duplicated here. The one piece of validation that HAS to live on this side is the dollars to
// micro-USD conversion, because the API only speaks micro-USD integers and a bad amount must be
// caught before it becomes a meaningless 422.
//
// Every action returns the refreshed list alongside its result. The table is the user's confirmation
// that the write landed, and re-reading it from the gateway proves that rather than assuming it: an
// upsert that succeeded but stored something different from what the form thought it sent shows up
// immediately instead of at the next page load.
import { revalidatePath } from "next/cache";

import {
  type Budget,
  deleteBudget,
  dollarsToMicro,
  queryBudgets,
  saveBudget,
} from "@/lib/budgets";

export interface BudgetFormValues {
  budgetId: string;
  period: string;
  /** Dollars as typed. Converted to integer micro-USD here, at the boundary. */
  amountDollars: string;
  scopeKind: string;
  scopeValue: string;
  startsOn: string;
  /** "" means open-ended, which is sent as null. */
  endsOn: string;
}

export interface BudgetActionResult {
  ok: boolean;
  /** Gateway text, verbatim. Only set when ok is false. */
  error?: string;
  /** Set on a 409 so the UI can offer to open the budget that was collided with. */
  conflictingBudgetId?: string | null;
  /** The tenant's budgets after the write. Null when we could not re-read them. */
  budgets?: Budget[] | null;
}

/** Re-read after a write. A failure here does not undo the write, so it is reported as an absent
 *  list rather than as a failed action. */
async function currentBudgets(): Promise<Budget[] | null> {
  const list = await queryBudgets();
  return list.reachable ? list.budgets : null;
}

export async function saveBudgetAction(values: BudgetFormValues): Promise<BudgetActionResult> {
  const amount = dollarsToMicro(values.amountDollars);
  if (!amount.ok) return { ok: false, error: amount.error, conflictingBudgetId: null };

  const result = await saveBudget({
    budgetId: values.budgetId.trim(),
    period: values.period,
    amountMicro: amount.micro,
    scopeKind: values.scopeKind,
    // A tenant-wide budget must name nothing; the gateway rejects the pair otherwise.
    scopeValue: values.scopeKind === "tenant" ? "" : values.scopeValue.trim(),
    startsOn: values.startsOn.trim(),
    endsOn: values.endsOn.trim() ? values.endsOn.trim() : null,
  });
  if (!result.ok) {
    return { ok: false, error: result.error, conflictingBudgetId: result.conflictingBudgetId };
  }
  revalidatePath("/settings/budgets");
  return { ok: true, budgets: await currentBudgets() };
}

export async function deleteBudgetAction(budgetId: string): Promise<BudgetActionResult> {
  const result = await deleteBudget(budgetId);
  if (!result.ok) {
    return { ok: false, error: result.error, conflictingBudgetId: result.conflictingBudgetId };
  }
  revalidatePath("/settings/budgets");
  return { ok: true, budgets: await currentBudgets() };
}
