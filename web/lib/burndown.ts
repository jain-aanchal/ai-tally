// SPDX-License-Identifier: Apache-2.0
// The burn-down view model (CTO-210, F6). Pure functions, no I/O, no clock.
//
// This is the payoff of the spend-forecasting epic: the projection from `forecast.ts` (CTO-206) fed
// with the settled series from `querySettledCostSeries` (CTO-207) and the budget from CTO-205, in
// the shape the chart and the card need. It COMPUTES NO FORECAST OF ITS OWN. `forecastSpend`
// already built the cone, the band and the breach date for this ticket, so re-deriving any of them
// here would produce a second answer to the same question and the two would drift.
//
// WHAT THIS FILE IS ACTUALLY FOR, given that:
//
//  1. Choosing the engine's inputs honestly. Only SETTLED days go in (CTO-207's rule), and the
//     window those days form is carried out again as `window` so the section can print it. A
//     forecast that cannot state what it measured is not auditable, and on live data the settled
//     window is around 10 percent smaller than the observed one, which is far too big a gap to
//     leave as a footnote (scope Decision 3).
//
//  2. THE TWO CLOCKS. Every date here comes from ClickHouse: `periodStart` and `windowEnd` are read
//     off the series, `asOf` is the series' own `settledThrough`, and the period end is calendar
//     arithmetic on `periodStart`. There is no `new Date()` anywhere in this module and none in the
//     chart. `queryCostSeries` still builds its axis from the Node clock (CTO-203) and the failure
//     mode is silent: the oldest day drops off the chart while still counting in the total printed
//     beside it. `chartReconciles` below exists so that if this section ever acquires the same bug
//     it says so on screen instead of quietly disagreeing with the card above it.
//
//  3. TRIMMING THE PRE-ONBOARDING ZEROS, which is what makes the minimum-history guard fire at all.
//     `querySettledCostSeries` gives every calendar day in its 30-day window a slot, and a day with
//     no rows is a real zero for the scope rather than a hole. That is right for the measured card:
//     a day a tenant spent nothing did cost nothing. It is WRONG as forecast history. A tenant
//     onboarded six days ago comes back with 29 settled days, 23 of which are zeros that mean "this
//     tenant did not exist yet", and the engine would happily project from them: past the history
//     floor, and with a weekday profile whose medians are mostly zero. That is precisely the
//     day-2 projection the floor exists to refuse, wearing a full month of fake history. So the
//     baseline starts at the first day the tenant was observed spending ANYTHING, and everything
//     before that is dropped rather than counted as evidence. `trimmedLeadingDays` reports how many
//     went, so the card can say so rather than silently shortening its own input window.
//
//  4. The layer split, which is what makes the forecast actionable. "You will land 12 percent over"
//     is a fact; "and compute is the reason" is something a reader can do something about. Each
//     layer is projected by the SAME engine over that layer's own settled history, so a layer with
//     a different weekday shape than the tenant total gets its own shape rather than a share of the
//     total. The cost of that choice is that independent medians do not add up: the layer
//     projections need not sum to the tenant projection, so `layersReconcile` is computed and the
//     card prints the difference rather than hiding it.
//
// Money is integer micro-USD. Dates are `YYYY-MM-DD` UTC strings.

import {
  daysBetweenInclusive,
  endOfMonth,
  selectBudget,
  type AppliedBudget,
  type SettledSeriesLike,
  type TenantBudget,
} from "./budgetVsActual";
import { LAYERS, type Layer } from "./cost";
import {
  forecastSpend,
  MIN_HISTORY_DAYS,
  type ForecastStatus,
  type SpendForecast,
} from "./forecast";
import type { MicroUSD } from "./types";

/**
 * Which days fed the projection, and which days inside the period it refused to use.
 *
 * All of it is restated from the series rather than recomputed from the forecast, because the point
 * of the line is to describe the INPUT. `dayCount` is the count the engine's minimum-history guard
 * is compared against, so `requiredDays` travels with it: "11 of the 14 days needed" is a readable
 * refusal, "not enough history" is not.
 */
export interface ForecastInputWindow {
  /** Oldest settled day fed in, or null when none has settled. */
  from: string | null;
  /** Newest settled day fed in, or null when none has settled. This is also `asOf`. */
  through: string | null;
  /** How many settled days fed in. Not `through - from`: a mid-window day can be withheld. */
  dayCount: number;
  /** False when a day inside the fed range was withheld, which is more surprising than a lagging tail. */
  contiguous: boolean;
  /** Which settled rule applied, verbatim from CTO-207. */
  rule: "connector-landing" | "day-complete";
  /** Connector layers settlement waits on. Empty under the `day-complete` rule. */
  waitsOn: readonly string[];
  /** Days inside the period that were excluded because they have not settled. */
  excludedDays: string[];
  /** Dollars those excluded days carry so far. Quantified, never merely asserted. */
  excludedMicroUsd: MicroUSD;
  /** Share of observed month-to-date withheld by settlement, or null when nothing was observed. */
  excludedShareOfObserved: number | null;
  /** `MIN_HISTORY_DAYS`, echoed so the UI never hard-codes the floor it is explaining. */
  requiredDays: number;
  /**
   * Days dropped from the front of the window because the tenant had not been observed spending
   * anything yet. See note 3 at the top of this file: these are almost always pre-onboarding, and
   * counting them as evidence is how a six-day-old tenant gets a month-long forecast.
   */
  trimmedLeadingDays: number;
  /** First day the tenant was observed spending anything, or null when it never was. */
  firstObservedDay: string | null;
}

/** One layer's own projection for the period. */
export interface LayerProjection {
  layer: Layer;
  /** Settled month-to-date spend for this layer. Measured. */
  settledMicroUsd: MicroUSD;
  /** Projected period total for this layer, or null when its own history is too short. */
  projectedMicroUsd: MicroUSD | null;
  /** This layer's share of the summed layer projections, or null when that sum is zero/unknown. */
  shareOfProjected: number | null;
  /** A layer-scoped budget when one is configured (CTO-205 `scope_kind='layer'`), else null. */
  budgetMicroUsd: MicroUSD | null;
  /** Projected minus layer budget. Positive is over. Null without a layer budget or a projection. */
  varianceMicroUsd: MicroUSD | null;
  /** Per-layer, because a quiet layer can fall below the history floor while the tenant does not. */
  status: ForecastStatus;
}

export interface BurndownSection {
  /** Every date here is ClickHouse's, never the Node clock's. See note 2 at the top of this file. */
  period: {
    /** First day of the calendar month, from the series. */
    start: string;
    /** Last day of that month, by arithmetic on `start`. */
    end: string;
    /** Last SETTLED day: where the actual line stops and the cone begins. Null when none settled. */
    asOf: string | null;
    /** Today per ClickHouse. Later than `asOf` whenever settlement is lagging, which is normal. */
    today: string;
  };
  /** The engine's answer, untouched. The cone, the band and the breach date all live in here. */
  forecast: SpendForecast;
  /** The one monthly tenant-wide budget covering today, or null when none is set. */
  budget: AppliedBudget | null;
  /** Non-null exactly when `budget` is null. A real sentence, for `<Blank reason>`. */
  noBudgetReason: string | null;
  /** Projected period spend minus budget. Positive is over. Null without both. */
  varianceMicroUsd: MicroUSD | null;
  /** As a fraction of budget. Null without a budget, and null when the budget is zero. */
  variancePct: number | null;
  window: ForecastInputWindow;
  /** Every layer, ordered by projected spend descending (settled spend when nothing projected). */
  layers: LayerProjection[];
  /** Sum of the layer projections, or null when no layer projected. */
  layerSumMicroUsd: MicroUSD | null;
  /**
   * True when the layer projections sum to the tenant projection. Usually FALSE by construction:
   * independent per-layer medians do not add. The card prints the gap rather than implying the
   * rows decompose the headline exactly.
   */
  layersReconcile: boolean;
  /**
   * The largest layer by projection: the "and compute is the reason" half of the answer. Null when
   * nothing projected.
   */
  largestLayer: Layer | null;
  /**
   * Cumulative actual at the last plotted actual point, i.e. what the CHART says was spent. Null
   * when no day of the period has settled and the chart therefore plots no actual at all.
   */
  chartActualMicroUsd: MicroUSD | null;
  /** Settled month-to-date from the series: what the card beside the chart prints. */
  settledPeriodMicroUsd: MicroUSD;
  /**
   * Whether the chart's own days sum to the figure printed next to it. The two-clocks bug (CTO-203)
   * makes these disagree silently by dropping a day off the axis, so the section states it out loud
   * the way the account detail view states its reconciliation.
   */
  chartReconciles: boolean;
}

function ratio(part: number, whole: number): number | null {
  // Zero denominator is undefined, not infinite and not zero. Callers render a blank.
  return whole === 0 ? null : part / whole;
}

/**
 * Build the burn-down section from a settled series and this tenant's budgets.
 *
 * The caller handles ClickHouse being unreachable (`series === null`) before getting here: there is
 * no honest projection from no data, and inventing one next to a real budget is the single worst
 * thing this surface could do.
 */
export function burndownSection(
  series: SettledSeriesLike,
  budgets: readonly TenantBudget[],
): BurndownSection {
  const periodStart = series.periodStart;
  const periodEnd = endOfMonth(periodStart);
  const today = series.windowEnd;

  // The last settled day is where the measured line stops. When nothing has settled we still have
  // to hand the engine an `asOf`; today is the honest one (nothing is measured through today), and
  // with an empty day list the engine refuses on the history floor anyway.
  const asOf = series.settledThrough;
  const asOfForEngine = asOf ?? today;

  // Note 3: history starts where the tenant does. Any observed spend counts, settled or not, so a
  // still-unsettled first day does not push the start of history a day later than it really is.
  const firstObservedDay = series.days.find((d) => d.totalMicroUsd !== 0)?.date ?? null;
  const allSettled = series.days.filter((d) => d.settled);
  const settled =
    firstObservedDay === null ? [] : allSettled.filter((d) => d.date >= firstObservedDay);
  const trimmedLeadingDays = allSettled.length - settled.length;
  const inPeriod = series.days.filter((d) => d.inPeriod);
  const settledInPeriod = inPeriod.filter((d) => d.settled);
  const excluded = inPeriod.filter((d) => !d.settled);

  const observedMicroUsd = series.periodObserved.totalMicroUsd;
  const settledPeriodMicroUsd = series.periodSettled.totalMicroUsd;
  const excludedMicroUsd = observedMicroUsd - settledPeriodMicroUsd;

  const from = settled[0]?.date ?? null;
  const through = settled[settled.length - 1]?.date ?? null;
  const window: ForecastInputWindow = {
    from,
    through,
    dayCount: settled.length,
    contiguous:
      from === null || through === null
        ? true
        : daysBetweenInclusive(from, through) === settled.length,
    rule: series.rule,
    waitsOn: series.connectorLayers,
    excludedDays: excluded.map((d) => d.date),
    excludedMicroUsd,
    excludedShareOfObserved: ratio(excludedMicroUsd, observedMicroUsd),
    requiredDays: MIN_HISTORY_DAYS,
    trimmedLeadingDays,
    firstObservedDay,
  };

  const tenantBudget = selectBudget(budgets, "tenant", "", today);
  const budget: AppliedBudget | null = tenantBudget
    ? {
        budgetId: tenantBudget.budget_id,
        amountMicroUsd: tenantBudget.amount_micro,
        startsOn: tenantBudget.starts_on,
        endsOn: tenantBudget.ends_on,
        coversPeriodToDate: tenantBudget.starts_on <= periodStart,
      }
    : null;

  const forecast = forecastSpend({
    // Settled days only, and every settled day in the trailing window, not just this month's: the
    // weekday profile wants four weeks and the period is only as old as the month is.
    days: settled.map((d) => ({ date: d.date, microUsd: d.totalMicroUsd })),
    periodStart,
    periodEnd,
    asOf: asOfForEngine,
    budgetMicroUsd: budget ? budget.amountMicroUsd : null,
  });

  const layers = LAYERS.map((layer) => {
    const layerForecast = forecastSpend({
      days: settled.map((d) => ({ date: d.date, microUsd: d.byLayer[layer] })),
      periodStart,
      periodEnd,
      asOf: asOfForEngine,
      // Layer budgets are compared below rather than passed in: the engine's breach date for a
      // layer is not rendered anywhere, and passing a budget here would compute a breach nothing
      // reads. The variance is what the split needs.
      budgetMicroUsd: null,
    });
    const layerBudget = selectBudget(budgets, "layer", layer, today);
    const projected = layerForecast.projectedMicroUsd;
    return {
      layer,
      settledMicroUsd: series.periodSettled.byLayer[layer],
      projectedMicroUsd: projected,
      // Filled in below: the denominator is the sum of all the layer projections.
      shareOfProjected: null as number | null,
      budgetMicroUsd: layerBudget ? layerBudget.amount_micro : null,
      varianceMicroUsd:
        layerBudget && projected !== null ? projected - layerBudget.amount_micro : null,
      status: layerForecast.status,
    };
  });

  const projectedLayers = layers.filter((l) => l.projectedMicroUsd !== null);
  const layerSumMicroUsd =
    projectedLayers.length > 0
      ? projectedLayers.reduce((sum, l) => sum + (l.projectedMicroUsd ?? 0), 0)
      : null;
  for (const l of layers) {
    l.shareOfProjected =
      l.projectedMicroUsd === null || layerSumMicroUsd === null
        ? null
        : ratio(l.projectedMicroUsd, layerSumMicroUsd);
  }
  // Projected first, because that is what the section is about; settled spend breaks ties and
  // orders the rows sensibly when nothing projected at all.
  layers.sort(
    (a, b) =>
      (b.projectedMicroUsd ?? -1) - (a.projectedMicroUsd ?? -1) ||
      b.settledMicroUsd - a.settledMicroUsd,
  );

  const projected = forecast.projectedMicroUsd;
  const varianceMicroUsd =
    budget && projected !== null ? projected - budget.amountMicroUsd : null;

  // What the chart itself plots as spent: the last burn-down point that carries an actual.
  const plottedActuals = forecast.burndown.filter((p) => p.actualMicroUsd !== null);
  const chartActualMicroUsd =
    plottedActuals.length > 0
      ? (plottedActuals[plottedActuals.length - 1].actualMicroUsd as number)
      : null;

  return {
    period: { start: periodStart, end: periodEnd, asOf, today },
    forecast,
    budget,
    noBudgetReason: budget
      ? null
      : "no monthly tenant-wide budget is set, so there is nothing to project against",
    varianceMicroUsd,
    variancePct:
      budget && varianceMicroUsd !== null ? ratio(varianceMicroUsd, budget.amountMicroUsd) : null,
    window,
    layers,
    layerSumMicroUsd,
    layersReconcile: projected === null || layerSumMicroUsd === null
      ? true
      : layerSumMicroUsd === projected,
    largestLayer: layers.find((l) => l.projectedMicroUsd !== null)?.layer ?? null,
    chartActualMicroUsd,
    settledPeriodMicroUsd,
    // Only a chart that exists can disagree with the figure beside it. Below the history floor the
    // burn-down is empty on purpose and the card draws nothing, so there is no claim to reconcile;
    // asserting a mismatch there would print a bug report about a chart nobody rendered. Otherwise
    // "nothing plotted" agrees with "nothing settled" only when the settled total is zero too.
    chartReconciles:
      forecast.burndown.length === 0
        ? true
        : chartActualMicroUsd === null
          ? settledInPeriod.length === 0
          : chartActualMicroUsd === settledPeriodMicroUsd,
  };
}

/** What the section carries over the wire. Exactly one of the two fields is non-null. */
export interface ForecastPayload {
  section: BurndownSection | null;
  /** Why there is no section: ClickHouse or the gateway could not be read. Never "no budget". */
  unavailable: string | null;
}
