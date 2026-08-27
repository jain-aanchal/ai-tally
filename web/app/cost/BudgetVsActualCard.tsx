// SPDX-License-Identifier: Apache-2.0
// The month-to-date-versus-budget section of /cost (CTO-209, F5).
//
// A section on this page rather than a /forecast tab, per docs/spend-forecasting-scope.md: it is
// the same question the reader already came to /cost with, and a separate tab for one comparison is
// premature.
//
// THE VARIANCE SIGN IS THE POINT, so it is the largest thing here and it is stated in words
// ("over" / "under") as well as by sign and colour. Colour alone fails for a colourblind reader and
// a leading minus is easy to miss in a wall of dollars.
//
// NO PROJECTION HERE. Everything on this card already happened. The elapsed figure sits next to the
// variance precisely so "under budget" on day 3 cannot be misread as "on track": that judgement
// needs the day-of-week weighted projection and its confidence band, which is CTO-210.
//
// Three things this card must never do, each of which it would be easy to do by accident:
//   - compare against a budget of zero when none is set. `Blank` with the store's own reason, and a
//     link to where a budget is set, instead.
//   - print a figure without saying which days it covers. The actual is settled days only
//     (CTO-207), and the coverage line names them, counts them and names the rule that chose them.
//   - hide the excluded days. Settled month-to-date can be much smaller than observed
//     month-to-date, by around 10 percent on a tenant whose cloud connectors are still landing, so
//     without the exclusion line this figure contradicts the 30-day headline directly above it and
//     the page reads as broken. The gap is printed in dollars rather than asserted, because on some
//     tenants it is pennies and the copy must not claim a size it does not know.

import Link from "next/link";
import type { ReactNode } from "react";

import { Card } from "@/components/Card";
import { DataTable, type Column } from "@/components/DataTable";
import { Blank, Money, Pct } from "@/components/HonestValue";
import type { BudgetVsActual, LayerLine } from "@/lib/budgetVsActual";
import { LAYER_LABEL } from "@/lib/cost";

/** What `/api/cost/budget` returns. Exactly one of the two fields is non-null. */
export interface BudgetPayload {
  comparison: BudgetVsActual | null;
  /** Why there is no comparison: ClickHouse or the gateway could not be read. Never "no budget". */
  unavailable: string | null;
}

/**
 * Where a budget is set. A plain path, deliberately: CTO-208 owns that surface and is landing in
 * parallel, so this file imports nothing from it and the two tickets cannot collide. "No budget
 * set" without a way to set one is a dead end, which is why the blank always ships with this link.
 */
const BUDGET_SETTINGS_PATH = "/settings/budgets";

export function BudgetVsActualCard({ payload }: { payload: BudgetPayload }) {
  const c = payload.comparison;
  if (!c) {
    return (
      <Card title="Month to date vs budget">
        {/* A blank with the real reason, never a zero and never "no budget set": not being able
            to read the spend or the budget is a different state from not having one. */}
        <div className="text-sm text-muted">
          <Blank reason={payload.unavailable ?? "this comparison could not be computed"} /> No
          comparison: {payload.unavailable ?? "this comparison could not be computed"}.
        </div>
      </Card>
    );
  }

  const { budget, period } = c;
  const over = c.varianceMicroUsd !== null && c.varianceMicroUsd > 0;

  return (
    <Card title="Month to date vs budget">
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div>
          <Variance comparison={c} />
          {budget === null ? (
            <p className="mt-2 text-sm text-muted">
              Month to date is {" "}
              <span className="font-medium text-fg">
                <Money micro={c.actualMicroUsd} />
              </span>
              .{" "}
              <Link href={BUDGET_SETTINGS_PATH} className="text-accent underline">
                Set a monthly budget
              </Link>{" "}
              to see a variance here.
            </p>
          ) : null}
        </div>

        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4 lg:grid-cols-2 xl:grid-cols-4">
          <Stat label="Month to date">
            <Money micro={c.actualMicroUsd} />
          </Stat>
          <Stat label="Budget (month)">
            {budget ? (
              <Money micro={budget.amountMicroUsd} />
            ) : (
              <Blank reason={c.noBudgetReason ?? "no budget set"} />
            )}
          </Stat>
          <Stat label="Budget consumed">
            {c.consumedFraction === null ? (
              <Blank
                reason={
                  budget === null
                    ? (c.noBudgetReason ?? "no budget set")
                    : "the budget is zero, so a percentage of it is undefined"
                }
              />
            ) : (
              <Pct value={c.consumedFraction} />
            )}
          </Stat>
          <Stat label="Period elapsed">
            <span className="tabular-nums">
              day {period.daysElapsed} of {period.daysInPeriod}
            </span>
            <span className="ml-2 text-sm text-muted">
              <Pct value={period.elapsedFraction} digits={0} />
            </span>
          </Stat>
        </dl>
      </div>

      <Provenance comparison={c} />

      {budget && !budget.coversPeriodToDate ? (
        <p className="mt-3 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
          This budget starts {budget.startsOn}, after the period began on {period.start}. The actual
          above covers the whole month to date, so it includes spend from before the budget applied.
        </p>
      ) : null}

      <div className="mt-5">
        <h3 className="mb-2 text-xs uppercase tracking-wide text-muted">
          By layer{over ? ": where the overage is" : ""}
        </h3>
        <DataTable
          columns={layerColumns(c)}
          rows={c.layers}
          rowKey={(r) => r.layer}
          pageSize={0}
          empty="No layer spend in this period."
        />
      </div>
    </Card>
  );
}

/**
 * The headline. Magnitude, direction in words, and percent, or a blank with a reason and nothing
 * invented when no budget is set.
 */
function Variance({ comparison }: { comparison: BudgetVsActual }) {
  const { varianceMicroUsd, variancePct, budget, noBudgetReason } = comparison;
  if (varianceMicroUsd === null) {
    return (
      <div>
        <div data-testid="budget-variance-headline" className="text-3xl font-semibold">
          <Blank reason={noBudgetReason ?? "no budget set"} />
        </div>
        <div className="mt-1 text-sm text-muted">No budget to compare against</div>
      </div>
    );
  }
  // Exact zero is its own answer and reads oddly as either "over" or "under".
  const direction = varianceMicroUsd === 0 ? "on budget" : varianceMicroUsd > 0 ? "over" : "under";
  const tone =
    varianceMicroUsd > 0 ? "text-bad" : varianceMicroUsd < 0 ? "text-good" : "text-muted";
  const sign = varianceMicroUsd > 0 ? "+" : varianceMicroUsd < 0 ? "−" : "";
  return (
    <div>
      {/* Test hook: the variance sign is the load-bearing thing on this card, and it is assembled
          from several nodes, so the test asserts on this element rather than on a text fragment. */}
      <div
        data-testid="budget-variance-headline"
        className={`text-3xl font-semibold tabular-nums ${tone}`}
      >
        {direction === "on budget" ? (
          "Exactly on budget"
        ) : (
          <>
            {sign}
            <Money micro={Math.abs(varianceMicroUsd)} /> {direction}
          </>
        )}
      </div>
      <div className="mt-1 text-sm text-muted">
        {variancePct === null ? (
          <>
            <Blank reason="the budget is zero, so a percentage variance is undefined" /> of a{" "}
            <Money micro={budget?.amountMicroUsd ?? 0} /> budget
          </>
        ) : (
          <>
            <span className={tone}>
              {sign}
              <Pct value={Math.abs(variancePct)} />
            </span>{" "}
            of the month budget, measured, not projected
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Which days the actual covers and which it does not. This is the audit trail: without it the
 * figure above is a number with no stated input window, and it will not reconcile with the 30-day
 * total on the same page.
 */
function Provenance({ comparison }: { comparison: BudgetVsActual }) {
  const { coverage, excluded, period } = comparison;
  const waiting = coverage.waitsOn.length > 0 ? coverage.waitsOn.join(" and ") : null;
  const gapDays = excluded.days.filter((d) => !d.inProgress);
  return (
    <div className="mt-4 space-y-1 border-t border-edge pt-3 text-xs text-muted">
      <p>
        {coverage.dayCount === 0 ? (
          <>
            No day of {period.start.slice(0, 7)} has settled yet, so the month-to-date figure above
            is <Money micro={0} /> over zero days.
          </>
        ) : (
          <>
            Counts {coverage.dayCount} settled {coverage.dayCount === 1 ? "day" : "days"},{" "}
            {coverage.from} through {coverage.through}
            {coverage.contiguous ? "" : " (with gaps, see below)"}. A day counts once it is complete
            {waiting ? ` and ${waiting} have landed for it` : ""} (rule: {coverage.rule}).
          </>
        )}
      </p>
      {excluded.days.length > 0 ? (
        <p>
          Excludes {excluded.days.length} {excluded.days.length === 1 ? "day" : "days"} (
          {excluded.days.map((d) => d.date).join(", ")}) carrying{" "}
          <Money micro={excluded.microUsd} /> so far
          {excluded.shareOfObserved === null ? (
            ""
          ) : (
            <>
              , <Pct value={excluded.shareOfObserved} /> of observed month to date
            </>
          )}
          {gapDays.length > 0 && waiting ? `, still waiting on ${waiting}` : ""}.{" "}
          {/* Says the excluded days are the reason the two figures can differ, without claiming a
              size for the gap: on this tenant it is currently pennies and on another it is a tenth
              of the month. The dollars are printed just above, so the reader can see which. */}
          Those days are why this figure need not match the 30-day total above.
        </p>
      ) : (
        <p>Every day of the month to date has settled; nothing is excluded.</p>
      )}
      <p>
        Observed month to date, settled and unsettled together, is{" "}
        <Money micro={comparison.observedMicroUsd} />.
      </p>
    </div>
  );
}

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-0.5 text-base font-medium tabular-nums">{children}</dd>
    </div>
  );
}

/**
 * The layer split, which is what makes this actionable: "12 percent over and compute is the reason"
 * is something a reader can act on, a single total is not.
 *
 * "Share of budget" is each layer's month-to-date spend as a fraction of the TENANT budget, so the
 * rows read as a decomposition of the consumed figure above. The last two columns appear only when
 * layer-scoped budgets (CTO-205 `scope_kind='layer'`) exist, because six columns of blanks would
 * bury the two that carry the answer.
 */
function layerColumns(comparison: BudgetVsActual): Column<LayerLine>[] {
  const hasLayerBudgets = comparison.layers.some((l) => l.budgetMicroUsd !== null);
  const noBudget = comparison.noBudgetReason ?? "no budget set";
  const columns: Column<LayerLine>[] = [
    {
      key: "layer",
      header: "Layer",
      render: (r) => LAYER_LABEL[r.layer],
      sortValue: (r) => LAYER_LABEL[r.layer],
    },
    {
      key: "actual",
      header: "Month to date",
      align: "right",
      render: (r) => <Money micro={r.actualMicroUsd} />,
      sortValue: (r) => r.actualMicroUsd,
    },
    {
      key: "share",
      header: "Share of spend",
      align: "right",
      render: (r) =>
        r.shareOfActual === null ? (
          <Blank reason="nothing has settled this month, so there is no total to take a share of" />
        ) : (
          <Pct value={r.shareOfActual} />
        ),
      sortValue: (r) => r.shareOfActual,
    },
    {
      key: "shareOfBudget",
      header: "Share of budget",
      align: "right",
      render: (r) =>
        r.shareOfTenantBudget === null ? (
          <Blank
            reason={
              comparison.budget === null
                ? noBudget
                : "the budget is zero, so a percentage of it is undefined"
            }
          />
        ) : (
          <Pct value={r.shareOfTenantBudget} />
        ),
      sortValue: (r) => r.shareOfTenantBudget,
    },
  ];
  if (!hasLayerBudgets) return columns;
  return [
    ...columns,
    {
      key: "layerBudget",
      header: "Layer budget",
      align: "right",
      render: (r) => (
        <Money
          micro={r.budgetMicroUsd}
          reason="no budget is set for this layer specifically"
        />
      ),
      sortValue: (r) => r.budgetMicroUsd,
    },
    {
      key: "layerVariance",
      header: "Variance",
      align: "right",
      render: (r) =>
        r.varianceMicroUsd === null ? (
          <Blank reason="no budget is set for this layer specifically" />
        ) : (
          <span className={r.varianceMicroUsd > 0 ? "text-bad" : "text-good"}>
            {r.varianceMicroUsd > 0 ? "+" : r.varianceMicroUsd < 0 ? "−" : ""}
            <Money micro={Math.abs(r.varianceMicroUsd)} />
          </span>
        ),
      sortValue: (r) => r.varianceMicroUsd,
    },
  ];
}
