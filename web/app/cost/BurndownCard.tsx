// SPDX-License-Identifier: Apache-2.0
// The burn-down section of /cost (CTO-210, F6): projection, confidence cone, breach date.
//
// It sits directly under the month-to-date card (CTO-209) because the two answer halves of one
// question. That card is everything that has already happened; this one is the only thing on the
// page that talks about the future, and the seam between them has to be obvious.
//
// THE BREACH DATE IS THE PRODUCT. "You cross budget on the 22nd" beats any percentage and arrives
// days before a threshold alert would, so it is the largest thing in the section and it is printed
// in words as well as marked on the chart. Everything else here exists to let a reader decide
// whether to believe it.
//
// FOUR HONESTY REQUIREMENTS, all load-bearing, each of which this file would fail silently without:
//
//  1. NO CONE BELOW THE HISTORY FLOOR. `forecastSpend` returns `insufficient_history` and this card
//     renders no chart at all, states how many settled days it has out of the fourteen it needs,
//     and says explicitly that this is not the same as "you are fine". A day-2 projection is nearly
//     meaningless and drawing one anyway is the fastest way to lose a customer's trust for good.
//     Note what is NOT done here: there is no "preview" cone, no greyed-out chart, no reaching past
//     the status into `burndown` (which is empty in that state anyway). The refusal is the feature.
//
//  2. THE INPUT WINDOW IS STATED. Which days fed the projection, how many there were, that
//     unsettled days were excluded, and what those excluded days carry in dollars. On live data the
//     settled window differs from the naive one by 10 to 11 percent, which is far too large to be a
//     footnote: without this line the projection silently disagrees with the 30-day total further
//     up the page and the reader concludes the page is broken.
//
//  3. IT SAYS IT IS A FORECAST. Once finance sees this number it becomes something people are
//     measured against, so the section says out loud that it is a projection and not a commitment,
//     next to the number rather than in a tooltip.
//
//  4. THE FOUR BREACH OUTCOMES RENDER DIFFERENTLY. `never` ("we projected, and it stays under") and
//     `cannot_project` ("we did not project, so we are not claiming anything") are opposite
//     statements and collapsing them into one calm green line would be a lie by layout.
//
// The chart's day axis is built from ClickHouse's dates, never the Node clock (CTO-203). The card
// checks that the chart's own days still sum to the settled figure printed beside them and says so
// on screen when they do not, the way the account detail view does with its allocation rows.

import Link from "next/link";

import { BurndownChart, BurndownLegend } from "@/components/BurndownChart";
import { Card } from "@/components/Card";
import { DataTable, type Column } from "@/components/DataTable";
import { Blank, Money, Pct } from "@/components/HonestValue";
import type { BurndownSection, ForecastPayload, LayerProjection } from "@/lib/burndown";
import { LAYER_LABEL } from "@/lib/cost";

import type { BudgetPayload } from "./BudgetVsActualCard";

export type { ForecastPayload };

/**
 * The whole payload `/api/cost/budget` returns. One route, one ClickHouse read of the settled
 * series, both sections: the month-to-date comparison (CTO-209) and this forecast (CTO-210) are
 * computed from the SAME `querySettledCostSeries` result on purpose, so the measured figure and the
 * projected one can never be reading two different windows.
 */
export interface CostBudgetPayload extends BudgetPayload {
  forecast: ForecastPayload;
}

/** Where a budget is set. Same path the month-to-date card links to; CTO-208 owns that surface. */
const BUDGET_SETTINGS_PATH = "/settings/budgets";

export function BurndownCard({ payload }: { payload: ForecastPayload }) {
  const section = payload.section;
  if (!section) {
    return (
      <Card title="Projected month-end spend">
        <div className="text-sm text-muted">
          <Blank reason={payload.unavailable ?? "this forecast could not be computed"} /> No
          forecast: {payload.unavailable ?? "this forecast could not be computed"}.
        </div>
      </Card>
    );
  }

  const { forecast } = section;

  return (
    <Card title="Projected month-end spend">
      {forecast.status === "insufficient_history" ? (
        <InsufficientHistory section={section} />
      ) : (
        <>
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
            <Headline section={section} />
            <Figures section={section} />
          </div>

          <div className="mt-5">
            <BurndownChart
              points={forecast.burndown}
              budgetMicroUsd={section.budget ? section.budget.amountMicroUsd : null}
              breach={forecast.breach}
              naiveRunRateMicroUsd={forecast.naiveRunRateMicroUsd}
              asOf={section.period.asOf}
            />
            <BurndownLegend hasBudget={section.budget !== null} />
          </div>

          <LayerSplit section={section} />
        </>
      )}

      <Provenance section={section} />
    </Card>
  );
}

/**
 * The refusal. No chart, no cone, no number, and a reason that names the floor and the shortfall.
 *
 * The last sentence is the important one. Refusing to project is not a claim of safety, and a
 * reader who takes "no forecast" as "no problem" has been misled by us just as badly as one who
 * took a day-2 number seriously.
 */
function InsufficientHistory({ section }: { section: BurndownSection }) {
  const { window, period } = section;
  const short = Math.max(0, window.requiredDays - window.dayCount);
  return (
    <div data-testid="burndown-insufficient-history">
      <div className="text-2xl font-semibold text-muted">
        <Blank reason={`only ${window.dayCount} settled days of history, ${window.requiredDays} are needed`} />{" "}
        Not enough history to project
      </div>
      <p className="mt-2 max-w-prose text-sm text-muted">
        This tenant has {window.dayCount} settled{" "}
        {window.dayCount === 1 ? "day" : "days"} of history, and a projection needs{" "}
        {window.requiredDays}.
        {window.trimmedLeadingDays > 0 && window.firstObservedDay !== null ? (
          <>
            {" "}
            Spend was first seen on {window.firstObservedDay}, so the {window.trimmedLeadingDays}{" "}
            earlier {window.trimmedLeadingDays === 1 ? "day" : "days"} in the window are not counted
            as history: they are days before this tenant was sending anything, not days it spent
            nothing.
          </>
        ) : null} {short > 0 ? `${short} more ${short === 1 ? "day" : "days"} ` : "More history "}
        will settle it. Fourteen days is two full weeks, which is the point: below that every weekday
        has at most one observation and the day-of-week profile the projection rests on is a single
        data point per weekday rather than a median.
      </p>
      <p className="mt-2 max-w-prose text-sm text-muted">
        No cone is drawn here on purpose. A projection from this much data would be volatile enough
        to be actively misleading, and drawing it faintly would still put a number on screen that
        somebody would repeat in a meeting.
      </p>
      <p className="mt-2 max-w-prose text-sm text-warn">
        This is not a statement that spend is under control. We are saying we do not know, which is
        a different thing from &ldquo;this will not breach&rdquo;. Month to date is{" "}
        <Money micro={section.settledPeriodMicroUsd} /> across days {period.start} to{" "}
        {period.asOf ?? period.start}
        {section.budget ? (
          <>
            {" "}
            against a <Money micro={section.budget.amountMicroUsd} /> budget
          </>
        ) : null}
        .
      </p>
    </div>
  );
}

/**
 * The breach date, or the specific reason there is not one. Four outcomes, four different shapes.
 */
function Headline({ section }: { section: BurndownSection }) {
  const { breach } = section.forecast;
  const { budget, varianceMicroUsd, variancePct, period } = section;

  if (breach.outcome === "no_budget") {
    return (
      <div data-testid="burndown-headline">
        <div className="text-3xl font-semibold tabular-nums">
          <Money micro={section.forecast.projectedMicroUsd} reason="nothing was projected" />
        </div>
        <div className="mt-1 text-sm text-muted">
          projected for {period.start.slice(0, 7)}, a forecast and not a commitment
        </div>
        <p className="mt-3 max-w-prose text-sm text-muted">
          <Blank reason={section.noBudgetReason ?? "no budget set"} /> No budget is set, so there is
          no breach date and no variance: the projection is drawn without a reference line rather
          than compared against a budget of zero.{" "}
          <Link href={BUDGET_SETTINGS_PATH} className="text-accent underline">
            Set a monthly budget
          </Link>{" "}
          to get one.
        </p>
      </div>
    );
  }

  if (breach.outcome === "breaches" && breach.date !== null) {
    return (
      <div data-testid="burndown-headline">
        <div className="text-xs uppercase tracking-wide text-muted">Forecast breach date</div>
        <div className="mt-1 text-3xl font-semibold text-bad tabular-nums">
          Crosses budget {breach.date}
        </div>
        <p className="mt-2 max-w-prose text-sm">
          <span className="text-bad">
            {varianceMicroUsd !== null && varianceMicroUsd > 0 ? "+" : ""}
            <Money micro={varianceMicroUsd} reason="no budget to compare against" /> over
          </span>
          {variancePct === null ? null : (
            <>
              {" "}
              (<Pct value={variancePct} />)
            </>
          )}{" "}
          by {period.end}, on day {breach.dayIndex} of {section.forecast.daysInPeriod}. This is a
          forecast, not a commitment: it is what the last {section.window.dayCount} settled days
          imply, not a number anyone has agreed to.
        </p>
        {breach.earliestDate !== null && breach.earliestDate !== breach.date ? (
          <p className="mt-2 max-w-prose text-sm text-muted">
            The high edge of the band crosses as early as {breach.earliestDate}. That is the bad
            case, not the likely one.
          </p>
        ) : null}
      </div>
    );
  }

  // `never`: we DID project, and it stays under. Said in those words, because the contrast with
  // `cannot_project` (which never reaches this component) is the whole point of keeping them apart.
  return (
    <div data-testid="burndown-headline">
      <div className="text-xs uppercase tracking-wide text-muted">Forecast breach date</div>
      <div className="mt-1 text-3xl font-semibold text-good">None this period</div>
      <p className="mt-2 max-w-prose text-sm">
        <span className="text-good">
          <Money micro={varianceMicroUsd === null ? null : Math.abs(varianceMicroUsd)} reason="no budget to compare against" />{" "}
          under
        </span>
        {variancePct === null ? null : (
          <>
            {" "}
            (<Pct value={Math.abs(variancePct)} />)
          </>
        )}{" "}
        the <Money micro={budget?.amountMicroUsd ?? null} reason="no budget set" /> budget by{" "}
        {period.end}. The projection stays under budget for every remaining day. This is a forecast,
        not a commitment.
      </p>
      {breach.earliestDate !== null ? (
        <p className="mt-2 max-w-prose text-sm text-warn">
          The high edge of the band does cross, on {breach.earliestDate}. The likely path stays
          under; the bad case does not.
        </p>
      ) : null}
    </div>
  );
}

/** The supporting numbers, including the naive run-rate the chart draws as its sanity line. */
function Figures({ section }: { section: BurndownSection }) {
  const { forecast, budget } = section;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
      <Stat label="Projected month end">
        <Money micro={forecast.projectedMicroUsd} reason="nothing was projected" />
      </Stat>
      <Stat label="Range (80% band)">
        <span className="tabular-nums">
          <Money micro={forecast.lowMicroUsd} reason="nothing was projected" /> to{" "}
          <Money micro={forecast.highMicroUsd} reason="nothing was projected" />
        </span>
      </Stat>
      <Stat label="Budget (month)">
        <Money
          micro={budget ? budget.amountMicroUsd : null}
          reason={section.noBudgetReason ?? "no budget set"}
        />
      </Stat>
      <Stat label="Settled month to date">
        <Money micro={forecast.spendToDateMicroUsd} />
      </Stat>
      <Stat label="Naive run-rate">
        {/* The dumb sanity line, printed as well as drawn: a reader who distrusts the weekday
            weighting can compare the two without leaving the page. It is deliberately NOT the
            headline, per scope Decision 1. */}
        <Money micro={forecast.naiveRunRateMicroUsd} reason="nothing was projected" />
      </Stat>
      <Stat label="Days settled">
        <span className="tabular-nums">
          {forecast.daysElapsed} of {forecast.daysInPeriod}
        </span>
        <span className="ml-2 text-sm text-muted">{forecast.daysRemaining} to go</span>
      </Stat>
    </dl>
  );
}

/**
 * The layer split: the actionable half. A total says how bad; this says what to change.
 *
 * Each layer is projected independently by the same engine, so the rows do NOT have to sum to the
 * headline, and the note below says so with the actual difference in dollars rather than letting a
 * reader discover it by adding the column up.
 */
function LayerSplit({ section }: { section: BurndownSection }) {
  const { layers, largestLayer, layerSumMicroUsd, forecast } = section;
  const largest = layers.find((l) => l.layer === largestLayer) ?? null;
  const over = section.varianceMicroUsd !== null && section.varianceMicroUsd > 0;
  const gap =
    layerSumMicroUsd === null || forecast.projectedMicroUsd === null
      ? null
      : layerSumMicroUsd - forecast.projectedMicroUsd;

  return (
    <div className="mt-5">
      <h3 className="mb-2 text-xs uppercase tracking-wide text-muted">
        By layer: what the projection is made of
      </h3>
      {largest && largest.projectedMicroUsd !== null ? (
        <p className="mb-2 max-w-prose text-sm">
          {over ? (
            <>
              You are projected to land{" "}
              <span className="text-bad">
                <Pct value={section.variancePct ?? 0} /> over
              </span>
              , and {LAYER_LABEL[largest.layer]} is the largest part of it:{" "}
            </>
          ) : (
            <>The largest projected layer is {LAYER_LABEL[largest.layer]}: </>
          )}
          <Money micro={largest.projectedMicroUsd} />
          {largest.shareOfProjected === null ? null : (
            <>
              , <Pct value={largest.shareOfProjected} /> of projected spend
            </>
          )}
          , up from <Money micro={largest.settledMicroUsd} /> settled so far this month.
        </p>
      ) : null}
      <DataTable
        columns={layerColumns(section)}
        rows={layers}
        rowKey={(r) => r.layer}
        pageSize={0}
        empty="No layer spend in this period."
      />
      <p className="mt-2 max-w-prose text-xs text-muted">
        Each layer is projected from its own settled history with its own day-of-week profile, so
        these rows are six independent projections rather than a split of the headline. Independent
        medians do not add: they sum to{" "}
        <Money micro={layerSumMicroUsd} reason="no layer could be projected" />
        {gap === null || gap === 0 ? (
          ", which happens to match the projection above exactly."
        ) : (
          <>
            , <Money micro={Math.abs(gap)} /> {gap > 0 ? "more" : "less"} than the{" "}
            <Money micro={forecast.projectedMicroUsd} reason="nothing was projected" /> projected
            above. That difference is the method, not a bug.
          </>
        )}
      </p>
    </div>
  );
}

function layerColumns(section: BurndownSection): Column<LayerProjection>[] {
  const hasLayerBudgets = section.layers.some((l) => l.budgetMicroUsd !== null);
  const columns: Column<LayerProjection>[] = [
    {
      key: "layer",
      header: "Layer",
      render: (r) => LAYER_LABEL[r.layer],
      sortValue: (r) => LAYER_LABEL[r.layer],
    },
    {
      key: "settled",
      header: "Settled to date",
      align: "right",
      render: (r) => <Money micro={r.settledMicroUsd} />,
      sortValue: (r) => r.settledMicroUsd,
    },
    {
      key: "projected",
      header: "Projected month end",
      align: "right",
      render: (r) => (
        <Money
          micro={r.projectedMicroUsd}
          reason="this layer has too little settled history of its own to project"
        />
      ),
      sortValue: (r) => r.projectedMicroUsd,
    },
    {
      key: "share",
      header: "Share of projection",
      align: "right",
      render: (r) =>
        r.shareOfProjected === null ? (
          <Blank reason="nothing was projected for this layer, so it has no share" />
        ) : (
          <Pct value={r.shareOfProjected} />
        ),
      sortValue: (r) => r.shareOfProjected,
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
        <Money micro={r.budgetMicroUsd} reason="no budget is set for this layer specifically" />
      ),
      sortValue: (r) => r.budgetMicroUsd,
    },
    {
      key: "layerVariance",
      header: "Projected variance",
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

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-0.5 text-base font-medium tabular-nums">{children}</dd>
    </div>
  );
}

/**
 * The audit trail: exactly which days fed the projection and which were withheld.
 *
 * This is requirement 2 at the top of the file. It is rendered in EVERY state, including the
 * refusal, because "we have 11 of 14 days" is the whole content of the refusal and because a reader
 * comparing this section against the 30-day headline higher up the page needs the same explanation
 * either way.
 */
function Provenance({ section }: { section: BurndownSection }) {
  const { window, period, forecast } = section;
  const waiting = window.waitsOn.length > 0 ? window.waitsOn.join(" and ") : null;
  return (
    <div className="mt-4 space-y-1 border-t border-edge pt-3 text-xs text-muted">
      <p>
        {window.dayCount === 0 ? (
          <>
            No day has settled yet, so nothing has fed a projection. A day counts once it is complete
            {waiting ? ` and ${waiting} have landed for it` : ""} (rule: {window.rule}).
          </>
        ) : (
          <>
            Projected from {window.dayCount} settled {window.dayCount === 1 ? "day" : "days"},{" "}
            {forecast.windowStart ?? window.from} through {forecast.windowEnd ?? window.through}
            {window.contiguous ? "" : " (with gaps)"}. A day counts once it is complete
            {waiting ? ` and ${waiting} have landed for it` : ""} (rule: {window.rule}). Unsettled
            days are excluded from the baseline: a half-reported day drags the run-rate down and
            makes the projection systematically low.
            {window.trimmedLeadingDays > 0 && window.firstObservedDay !== null ? (
              <>
                {" "}
                {window.trimmedLeadingDays} earlier{" "}
                {window.trimmedLeadingDays === 1 ? "day" : "days"} in the window {" "}
                {window.trimmedLeadingDays === 1 ? "was" : "were"} dropped: nothing was seen from
                this tenant before {window.firstObservedDay}, and a day it did not yet exist for is
                not evidence that it spends nothing.
              </>
            ) : null}
          </>
        )}
      </p>
      {window.excludedDays.length > 0 ? (
        <p>
          Excludes {window.excludedDays.length}{" "}
          {window.excludedDays.length === 1 ? "day" : "days"} of this month (
          {window.excludedDays.join(", ")}) carrying <Money micro={window.excludedMicroUsd} /> so far
          {window.excludedShareOfObserved === null ? null : (
            <>
              , <Pct value={window.excludedShareOfObserved} /> of observed month to date
            </>
          )}
          . Those days are why the figures here need not match the 30-day total above.
        </p>
      ) : (
        <p>Every day of the month to date has settled; nothing is excluded from the baseline.</p>
      )}
      <p>
        Period {period.start} to {period.end}, measured through {period.asOf ?? "no settled day yet"}
        ; today is {period.today} per the warehouse clock. The chart&rsquo;s day axis is built from
        those dates, not from this server&rsquo;s clock.
      </p>
      {/* The chart's days against the figure printed beside them. Normally dead code, kept for the
          same reason the account detail view keeps its reconciliation line: a silent disagreement
          here is exactly the CTO-203 failure mode, and admitting it beats hiding it. */}
      {section.chartReconciles ? null : (
        <p className="text-warn">
          The days plotted above sum to{" "}
          <Money micro={section.chartActualMicroUsd} reason="the chart plots no actual" />, but
          settled month to date is <Money micro={section.settledPeriodMicroUsd} />. That gap is a bug
          in this chart rather than a fact about your spend.
        </p>
      )}
      {section.budget && !section.budget.coversPeriodToDate ? (
        <p className="text-warn">
          This budget starts {section.budget.startsOn}, after the period began on {period.start}, so
          the projection covers spend from before the budget applied.
        </p>
      ) : null}
    </div>
  );
}
