// SPDX-License-Identifier: Apache-2.0
// Month-to-date actual versus budget (CTO-209, F5). Pure functions, no I/O, no clock.
//
// This is the MEASURED half of the spend-forecasting epic and deliberately nothing more. It ships
// before the projection (CTO-210) because it is useful immediately and cannot be wrong about the
// future: every number here is something that already happened. It also puts the budget somebody
// entered in CTO-205 in front of them, so they find out whether that number is right before a
// forecast starts depending on it.
//
// NOTHING IN THIS FILE PROJECTS. There is no run-rate, no pro-rating of the budget by elapsed days,
// no month-end estimate. A pro-rated "expected spend so far" looks measured but is a flat-run-rate
// projection wearing a disguise, and the scope doc is explicit that the projection is a separate,
// day-of-week weighted thing with a confidence band. So the variance here is exactly
// `month-to-date actual - full-period budget`, and the elapsed fraction is reported ALONGSIDE it so
// a reader can see that "under budget" on day 3 means very little. See `web/lib/forecast.ts` and
// docs/spend-forecasting-scope.md for the half that does project.
//
// THE THREE HONESTY RULES THIS MODULE ENFORCES
//
//  1. No budget row is a normal state, never an implicit zero. `budget` comes back null and every
//     variance field comes back null with `noBudgetReason` set, so the UI renders the actual spend
//     and a blank instead of reporting the tenant as infinitely over a budget of zero. This mirrors
//     the rule stated in `gateway/tenant_budgets.py`.
//
//  2. The figure states which days it covers. The actual is summed over SETTLED days only (the
//     rule from CTO-207, see `querySettledCostSeries`), and `coverage` names the first and last of
//     them and how many there are. A figure that cannot say what it measured is not auditable.
//
//  3. The excluded days are quantified, not just mentioned. `excluded` carries the dollars and the
//     share of observed month-to-date that settlement withheld. Without it this number silently
//     disagrees with the 30-day headline higher up the same page and the page looks broken. On the
//     demo tenant the gap is ~10 percent, which is far too big to leave unexplained.
//
// A stored budget of zero is a real claim ("this scope may spend nothing"), so it is compared
// normally in dollars; only the PERCENT variance is null, because dividing by it is undefined
// rather than infinite. Absence of a row is the thing that means "no budget".
//
// Money is integer micro-USD throughout. Dates are `YYYY-MM-DD` UTC strings supplied by the caller
// (ClickHouse's clock, via the series), never generated here: same input, same output, in tests and
// in CI.

import { LAYERS, type Layer } from "./cost";
import type { MicroUSD, SpendByLayer } from "./types";

/** Period we compare over. The series' period window is a calendar month, so this section is
 * month-only; a quarterly budget is not silently compared against a month of spend. */
export const COMPARED_PERIOD = "month";

/** One budget row as `GET /v1/tenant/budgets` returns it (CTO-205). Snake case is the wire shape. */
export interface TenantBudget {
  budget_id: string;
  period: string;
  amount_micro: number;
  scope_kind: string;
  scope_value: string;
  starts_on: string;
  ends_on: string | null;
}

/**
 * The fields of `SettledSpendSeries` (CTO-207) this comparison needs, restated structurally so this
 * module imports nothing from `clickhouse.ts` and stays trivially testable. A real
 * `SettledSpendSeries` satisfies it.
 */
export interface SettledSeriesLike {
  periodStart: string;
  windowEnd: string;
  settledThrough: string | null;
  rule: "connector-landing" | "day-complete";
  connectorLayers: readonly string[];
  days: readonly {
    date: string;
    byLayer: SpendByLayer;
    totalMicroUsd: MicroUSD;
    inPeriod: boolean;
    settled: boolean;
    inProgress: boolean;
    awaitingLayers: readonly string[];
  }[];
  periodSettled: { dayCount: number; byLayer: SpendByLayer; totalMicroUsd: MicroUSD };
  periodObserved: { dayCount: number; byLayer: SpendByLayer; totalMicroUsd: MicroUSD };
}

/** Which days the actual was summed over. Days may be non-contiguous, hence a count as well. */
export interface CoverageWindow {
  /** Oldest settled day inside the period, or null when none has settled yet. */
  from: string | null;
  /** Newest settled day inside the period, or null when none has settled yet. */
  through: string | null;
  /** How many days were counted. Not `through - from`: a mid-month day can be withheld. */
  dayCount: number;
  /** True when the counted days run without a gap. False means a day inside the range was withheld. */
  contiguous: boolean;
  /** Which settled rule applied, verbatim from CTO-207. */
  rule: "connector-landing" | "day-complete";
  /** Connector layers settlement waits on. Empty under the `day-complete` rule. */
  waitsOn: readonly string[];
}

/** A day inside the period that the actual does NOT include, and why. */
export interface ExcludedDay {
  date: string;
  /** What has landed for it so far. Shown so the exclusion is quantified rather than asserted. */
  observedMicroUsd: MicroUSD;
  /** True for today, which is still accruing by definition. */
  inProgress: boolean;
  /** Connector layers with no row for this day yet. Empty when `inProgress`. */
  awaitingLayers: readonly string[];
}

export interface ExcludedSummary {
  days: ExcludedDay[];
  /** Dollars withheld: observed month-to-date minus settled month-to-date. */
  microUsd: MicroUSD;
  /** Share of OBSERVED month-to-date that is withheld, or null when nothing has been observed. */
  shareOfObserved: number | null;
}

/** Calendar progress through the period. Measured, not projected. */
export interface PeriodProgress {
  /** First day of the calendar month. */
  start: string;
  /** Last day of the calendar month. */
  end: string;
  /** Today per ClickHouse. */
  today: string;
  daysInPeriod: number;
  /** Days from `start` through `today` INCLUSIVE, so today counts as the day in progress. */
  daysElapsed: number;
  /** `daysElapsed / daysInPeriod`, 0..1. */
  elapsedFraction: number;
}

/** One cost layer's line in the split. */
export interface LayerLine {
  layer: Layer;
  /** Settled month-to-date spend for this layer. */
  actualMicroUsd: MicroUSD;
  /** This layer's share of settled month-to-date spend, or null when the total is zero. */
  shareOfActual: number | null;
  /** This layer's spend as a share of the TENANT budget, or null when no tenant budget applies. */
  shareOfTenantBudget: number | null;
  /** A layer-scoped budget when one is configured (CTO-205 scope_kind='layer'), else null. */
  budgetMicroUsd: MicroUSD | null;
  /** Positive = over this layer's own budget. Null when no layer budget is configured. */
  varianceMicroUsd: MicroUSD | null;
  /** Null when no layer budget applies, or when that budget is zero (undefined, not infinite). */
  variancePct: number | null;
}

export interface AppliedBudget {
  budgetId: string;
  amountMicroUsd: MicroUSD;
  startsOn: string;
  endsOn: string | null;
  /**
   * True when the budget covered the whole month-to-date span. False means it started mid-period,
   * so a full-period budget is being compared against spend from before it existed, and the UI has
   * to say so rather than quietly report the variance.
   */
  coversPeriodToDate: boolean;
}

export interface BudgetVsActual {
  period: PeriodProgress;
  /** Settled month-to-date spend. THE actual: every other figure hangs off it. */
  actualMicroUsd: MicroUSD;
  /** Month-to-date including unsettled days. Diagnostic, never the headline. */
  observedMicroUsd: MicroUSD;
  coverage: CoverageWindow;
  excluded: ExcludedSummary;
  /** The one monthly tenant-wide budget covering today, or null when none is set. */
  budget: AppliedBudget | null;
  /** Non-null exactly when `budget` is null. A real sentence, for `<Blank reason>`. */
  noBudgetReason: string | null;
  /** Positive = over budget. Null when no budget applies. The sign is the point of this section. */
  varianceMicroUsd: MicroUSD | null;
  /** Variance as a fraction of budget. Null with no budget, and null when the budget is zero. */
  variancePct: number | null;
  /** Fraction of budget consumed so far. Null with no budget, and null when the budget is zero. */
  consumedFraction: number | null;
  /** Every layer, ordered by settled spend descending. Zero layers are kept: a real zero. */
  layers: LayerLine[];
}

const MS_PER_DAY = 86_400_000;

function utc(date: string): number {
  return Date.parse(`${date}T00:00:00Z`);
}

/** Inclusive day count from `from` through `to`. Both are `YYYY-MM-DD` UTC. */
export function daysBetweenInclusive(from: string, to: string): number {
  return Math.round((utc(to) - utc(from)) / MS_PER_DAY) + 1;
}

/** Last calendar day of the month containing `date`. */
export function endOfMonth(date: string): string {
  const [y, m] = date.split("-").map(Number);
  // Day 0 of the next month is the last day of this one.
  return new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
}

/**
 * Calendar progress through the period. `today` counts as elapsed: it is the day in progress, and
 * "day 26 of 31" is how a reader thinks about it. The UI states the day numbers next to the
 * fraction so nobody has to guess whether the in-progress day was counted.
 */
export function periodProgress(periodStart: string, today: string): PeriodProgress {
  const end = endOfMonth(periodStart);
  const daysInPeriod = daysBetweenInclusive(periodStart, end);
  const daysElapsed = Math.min(Math.max(daysBetweenInclusive(periodStart, today), 0), daysInPeriod);
  return {
    start: periodStart,
    end,
    today,
    daysInPeriod,
    daysElapsed,
    elapsedFraction: daysInPeriod > 0 ? daysElapsed / daysInPeriod : 0,
  };
}

/**
 * The one budget covering `onDate` for a scope, or null.
 *
 * CTO-205 refuses overlapping budgets at write time with a gist EXCLUDE constraint, so at most one
 * row can match. The sort is not a tie-break policy, then: it is only there so that if that
 * invariant is ever broken (a bad backfill, a dropped constraint) this renders deterministically
 * instead of flickering between two budgets on alternate polls. Newest start wins because it is the
 * most recent statement of intent.
 *
 * Only `period === 'month'` is considered. The series' period window is a calendar month, so
 * comparing a quarterly budget here would put a quarter's dollars against a month of spend and
 * report every tenant as comfortably under.
 */
export function selectBudget(
  budgets: readonly TenantBudget[],
  scopeKind: string,
  scopeValue: string,
  onDate: string,
): TenantBudget | null {
  const matches = budgets
    .filter(
      (b) =>
        b.period === COMPARED_PERIOD &&
        b.scope_kind === scopeKind &&
        b.scope_value === scopeValue &&
        b.starts_on <= onDate &&
        (b.ends_on === null || b.ends_on >= onDate),
    )
    .sort((a, b) =>
      a.starts_on === b.starts_on
        ? a.budget_id.localeCompare(b.budget_id)
        : b.starts_on.localeCompare(a.starts_on),
    );
  return matches[0] ?? null;
}

function ratio(part: number, whole: number): number | null {
  // Zero denominator is undefined, not infinite, and not zero. Callers render a blank.
  return whole === 0 ? null : part / whole;
}

/**
 * Month-to-date actual versus budget, split by layer.
 *
 * `series` is `querySettledCostSeries()`'s result and `budgets` is `GET /v1/tenant/budgets`. The
 * caller handles the ClickHouse-unreachable case (`series === null`) before getting here: this
 * function has no way to render "we do not know" and must not invent a zero for it.
 */
export function budgetVsActual(
  series: SettledSeriesLike,
  budgets: readonly TenantBudget[],
): BudgetVsActual {
  const period = periodProgress(series.periodStart, series.windowEnd);

  const inPeriod = series.days.filter((d) => d.inPeriod);
  const settledDays = inPeriod.filter((d) => d.settled).map((d) => d.date);
  const excludedDays: ExcludedDay[] = inPeriod
    .filter((d) => !d.settled)
    .map((d) => ({
      date: d.date,
      observedMicroUsd: d.totalMicroUsd,
      inProgress: d.inProgress,
      awaitingLayers: d.awaitingLayers,
    }));

  const actualMicroUsd = series.periodSettled.totalMicroUsd;
  const observedMicroUsd = series.periodObserved.totalMicroUsd;
  const excludedMicroUsd = observedMicroUsd - actualMicroUsd;

  const from = settledDays[0] ?? null;
  const through = settledDays[settledDays.length - 1] ?? null;
  const coverage: CoverageWindow = {
    from,
    through,
    dayCount: settledDays.length,
    // Non-contiguous means a day INSIDE the counted range was withheld, which is a different and
    // more surprising statement than "the last day or two have not landed".
    contiguous:
      from === null || through === null
        ? true
        : daysBetweenInclusive(from, through) === settledDays.length,
    rule: series.rule,
    waitsOn: series.connectorLayers,
  };

  const tenantBudget = selectBudget(budgets, "tenant", "", period.today);
  const budget: AppliedBudget | null = tenantBudget
    ? {
        budgetId: tenantBudget.budget_id,
        amountMicroUsd: tenantBudget.amount_micro,
        startsOn: tenantBudget.starts_on,
        endsOn: tenantBudget.ends_on,
        coversPeriodToDate: tenantBudget.starts_on <= period.start,
      }
    : null;

  const layers: LayerLine[] = LAYERS.map((layer) => {
    const layerActual = series.periodSettled.byLayer[layer];
    const layerBudget = selectBudget(budgets, "layer", layer, period.today);
    return {
      layer,
      actualMicroUsd: layerActual,
      shareOfActual: ratio(layerActual, actualMicroUsd),
      shareOfTenantBudget: budget ? ratio(layerActual, budget.amountMicroUsd) : null,
      budgetMicroUsd: layerBudget ? layerBudget.amount_micro : null,
      varianceMicroUsd: layerBudget ? layerActual - layerBudget.amount_micro : null,
      variancePct: layerBudget
        ? ratio(layerActual - layerBudget.amount_micro, layerBudget.amount_micro)
        : null,
    };
  }).sort((a, b) => b.actualMicroUsd - a.actualMicroUsd);

  return {
    period,
    actualMicroUsd,
    observedMicroUsd,
    coverage,
    excluded: {
      days: excludedDays,
      microUsd: excludedMicroUsd,
      shareOfObserved: ratio(excludedMicroUsd, observedMicroUsd),
    },
    budget,
    noBudgetReason: budget
      ? null
      : "no monthly tenant-wide budget is set, so there is nothing to compare this against",
    varianceMicroUsd: budget ? actualMicroUsd - budget.amountMicroUsd : null,
    variancePct: budget ? ratio(actualMicroUsd - budget.amountMicroUsd, budget.amountMicroUsd) : null,
    consumedFraction: budget ? ratio(actualMicroUsd, budget.amountMicroUsd) : null,
    layers,
  };
}
