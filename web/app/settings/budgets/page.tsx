// SPDX-License-Identifier: Apache-2.0
// Budget settings (CTO-208, F4).
//
// WHERE THIS LIVES AND WHY. Budgets are configuration, not a reading: a number the tenant asserts,
// which every "versus budget" figure downstream then depends on. That puts it with the other things
// a tenant declares about themselves rather than on a spend surface, so it is its own route under
// /settings instead of a panel bolted onto /cost. It also keeps this out of the way of CTO-209,
// which owns /cost and is adding the budget-vs-actual section there; that page links here by route
// and neither page imports the other's components.
//
// The page separates three states that are easy to collapse into one and must not be:
//   * budgets exist            -> the table
//   * no budget set            -> normal, its own copy, no variance implied anywhere
//   * the gateway is unreachable -> we could not ask, which is NOT "no budget set"
import Link from "next/link";

import { Card } from "@/components/Card";
import { queryBudgets } from "@/lib/budgets";

import { BudgetManager } from "./BudgetManager";

export const dynamic = "force-dynamic";

export default async function BudgetSettingsPage() {
  const { budgets, configured, periods, scopeKinds, reachable, error } = await queryBudgets();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Settings — Budgets</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          What you intend to spend on AI, per period and per scope. This is the reference every
          &ldquo;versus budget&rdquo; figure is measured against. Until a budget exists here, spend
          surfaces report actuals and no variance rather than assuming a budget of zero.
        </p>
      </div>

      {!reachable && (
        // Distinct from the empty table on purpose. "We could not ask" must never be rendered as
        // "no budget set": one of those is a fact about the tenant, the other about our plumbing.
        <div className="rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
          <span className="font-medium">Budgets could not be read.</span>{" "}
          <span className="text-warn/90">
            {error ?? "the gateway did not answer"}. This is not the same as having no budget set,
            so nothing below should be taken as the current configuration.
          </span>
        </div>
      )}

      <Card
        title={
          reachable
            ? configured
              ? `Budgets — ${budgets.length} set`
              : "Budgets — none set"
            : "Budgets — unknown"
        }
      >
        <p className="mb-3 max-w-prose text-xs text-muted">
          One budget covers one scope for one period over one date range. Two budgets for the same
          scope and period cannot cover the same day, so there is never a question about which
          number a burn-down is drawn against. A tenant-wide budget and a feature budget for the
          same month are independent and do not collide.
        </p>
        <BudgetManager
          initialBudgets={budgets}
          periods={periods}
          scopeKinds={scopeKinds}
          reachable={reachable}
        />
      </Card>

      <p className="text-xs text-muted">
        Spend against these budgets appears on{" "}
        <Link href="/cost" className="text-accent hover:underline">
          Cost
        </Link>
        .
      </p>
    </div>
  );
}
