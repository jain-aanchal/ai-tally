// SPDX-License-Identifier: Apache-2.0
// Spend forecasting engine (CTO-206). Pure functions, no I/O.
//
// This answers a calendar question: "given what this tenant has spent so far, where does the period
// land, and when does it cross budget?" That is NOT the question `sdk/python/src/tally/projection.py`
// answers. That module does pre-deploy change impact (p99 cost per run over a replayed sample) and
// none of it is reusable here. What is worth stealing is its posture: report the honest uncertain
// thing rather than the flattering point estimate. Hence a band, a minimum-history refusal, and a
// stated input window rather than a single confident number.
//
// Three decisions, all from `docs/spend-forecasting-scope.md`:
//
//   1. **Day-of-week weighted median**, not a flat run-rate. AI spend has strong weekday/weekend
//      structure: a B2B product can run at a third of weekday volume on a Sunday. Projecting the
//      remaining days from a flat mean is wrong every weekend, and wrong in a direction that flips
//      depending on which day of the week the period happens to be paused on. The trailing profile
//      uses a MEDIAN per weekday so one spike day does not distort the whole period.
//      The naive run-rate is still computed and returned, but as a labelled sanity line, never as
//      the headline.
//
//   2. **A band that is wide early and narrow late.** A day-2 projection genuinely is nearly
//      meaningless and a day-25 one is nearly certain, so the width is driven both by the observed
//      variance of daily spend and by how much of the period is still unwritten. This is the
//      honesty mechanism of the whole feature: it is what stops a volatile day-3 number reading as
//      a commitment.
//
//   3. **Refuse below a minimum history.** Under MIN_HISTORY_DAYS settled days there is no
//      projection, no band, and no breach date, and the caller is told which of those it is. This
//      follows the `/compare` precedent, which renders a blank rather than a noisy score below its
//      replay floor. A forecast from four days of data must say "not enough history yet".
//
// Explicitly out of scope (scope Decision 1): Holt-Winters or any other time-series model. Most
// tenants have about 30 days of history, which does not support one, and it would look
// authoritative while being wrong.
//
// Money is integer micro-USD throughout (mirrors `unitEconomics.ts`, `allocation.ts` and
// `tally.pricing`); never float dollars. Every field distinguishes unknown (null) from zero, as the
// rest of this codebase does: a tenant that spent nothing and a tenant we cannot project for are
// different answers and the UI renders them differently.
//
// Dates are `YYYY-MM-DD` strings interpreted in UTC. No `Date.now()`, no local timezone: the caller
// supplies `asOf`, so the same input always produces the same output in tests and in CI.

/** One settled day of spend, integer micro-USD. */
export interface DailySpend {
  /** `YYYY-MM-DD`, UTC. */
  date: string;
  /** Settled spend for that day, integer micro-USD. May be negative (a credit). */
  microUsd: number;
}

/**
 * Minimum settled days of history before this engine will project at all.
 *
 * Fourteen, for two reasons. It is two full weeks, so every weekday has at least two observations
 * and a median per weekday is a median rather than a single point. And it is short enough that a
 * tenant onboarded at the start of a month sees a forecast inside that same month, which is when
 * the feature is worth anything. Below it the honest answer is "not enough history yet".
 */
export const MIN_HISTORY_DAYS = 14;

/**
 * How far back the weekday profile and the variance estimate look. Four weeks gives up to four
 * observations per weekday, which is enough for a stable median, while still tracking a tenant
 * whose spend stepped up a month ago rather than averaging over a level that no longer exists.
 */
export const TRAILING_WINDOW_DAYS = 28;

/**
 * Half-width multiplier on the standard deviation of daily spend. 1.2816 is the normal 80 percent
 * two-sided z. The band is a heuristic, not a distributional claim, so a wide-but-not-absurd
 * interval beats a 95 percent one that would swamp the chart on any noisy tenant.
 */
const CONFIDENCE_Z = 1.2816;

/**
 * Extra width applied in proportion to how much of the period is still unwritten. Variance alone
 * (which scales with the square root of the remaining days) narrows too slowly to express how
 * little a day-2 projection is worth, because early on the weekday profile itself is the shakiest
 * part and that uncertainty is not in the daily variance. This term is what makes the cone visibly
 * fat at the start of the period and pinch shut at the end.
 */
const EARLY_PERIOD_INFLATION = 1.5;

/** Whether the engine produced a projection, and if not, why not. */
export type ForecastStatus = "ok" | "insufficient_history";

/**
 * What happened to the budget, kept distinct on purpose.
 *
 * `breaches`   — the projection crosses the budget inside the period; `date` says when.
 * `never`      — we projected, and the projection stays under budget for the whole period.
 * `no_budget`  — no budget was configured. Not a forecast failure; render the forecast without a
 *                variance rather than assuming a budget of zero (scope: "honest under uncertainty").
 * `cannot_project` — below the minimum history. We are not saying it will not breach; we are saying
 *                we do not know. A caller MUST render this differently from `never`.
 */
export type BreachOutcome = "breaches" | "never" | "no_budget" | "cannot_project";

export interface BreachForecast {
  outcome: BreachOutcome;
  /** `YYYY-MM-DD` the projection crosses the budget, or null for every other outcome. */
  date: string | null;
  /** 1-based day index within the period for `date`, or null. Convenient for a chart x-axis. */
  dayIndex: number | null;
  /**
   * The earliest crossing along the HIGH edge of the band: the bad case, not the likely one. Null
   * when even the high edge stays under budget, or when we cannot project. Always on or before
   * `date` when both are set.
   */
  earliestDate: string | null;
  /** Echo of the budget the outcome was computed against. Null when none was configured. */
  budgetMicroUsd: number | null;
}

/** One day of the burn-down: cumulative actual up to `asOf`, cumulative projection after it. */
export interface BurndownPoint {
  date: string;
  /** Cumulative settled spend through this day, or null for days after `asOf`. */
  actualMicroUsd: number | null;
  /** Cumulative point projection through this day. Equals the actual on and before `asOf`. */
  projectedMicroUsd: number;
  /** Low edge of the cone. Equals the actual on and before `asOf` (the past has no band). */
  lowMicroUsd: number;
  /** High edge of the cone. */
  highMicroUsd: number;
}

export interface SpendForecast {
  status: ForecastStatus;

  /** Projected spend for the whole period, day-of-week weighted. Null below the history floor. */
  projectedMicroUsd: number | null;
  /** Low edge of the band at period end. Null below the history floor. */
  lowMicroUsd: number | null;
  /** High edge of the band at period end. Null below the history floor. */
  highMicroUsd: number | null;

  /**
   * `spend_so_far / days_elapsed * days_in_period`. The sanity line, shown ALONGSIDE the headline
   * so a reader can see what the weekday weighting did. Null below the history floor as well: it is
   * still a projection, and the point of the floor is that no projection ships from four days of
   * data, however simple its arithmetic.
   */
  naiveRunRateMicroUsd: number | null;

  /** Measured, not projected: settled spend inside the period through `asOf`. Never null. */
  spendToDateMicroUsd: number;

  /** Days of the period already settled (1-based count through `asOf`). */
  daysElapsed: number;
  /** Calendar days in the period, inclusive of both ends. */
  daysInPeriod: number;
  /** `daysInPeriod - daysElapsed`. Zero once the period has closed. */
  daysRemaining: number;

  /** Count of settled days fed in at or before `asOf`, whether or not they fall inside the period. */
  historyDays: number;
  /**
   * The window the weekday profile and the variance were actually computed from, inclusive. Null
   * when nothing was projected. A forecast that cannot state its input window is not auditable
   * (scope Decision 3), and late-arriving connector data makes that a live concern here: the caller
   * is expected to pass only SETTLED days, and this is what it can show for having done so.
   */
  windowStart: string | null;
  windowEnd: string | null;

  /**
   * Trailing median spend per weekday, indexed 0 = Sunday .. 6 = Saturday. An entry is null when
   * that weekday has no observation in the trailing window. Null (not zero) matters: a weekday we
   * have never seen is unknown, and the projection falls back to the overall median for it.
   */
  weekdayMedianMicroUsd: (number | null)[];

  /** Standard deviation of trailing daily spend, integer micro-USD. Null when not projected. */
  dailyStdDevMicroUsd: number | null;

  breach: BreachForecast;

  /** The burn-down series. Empty when nothing was projected. */
  burndown: BurndownPoint[];
}

export interface ForecastInput {
  /**
   * Settled daily spend. Order does not matter (sorted internally) but dates must be unique, and
   * days should be SETTLED: a half-reported final day drags the whole baseline down and makes the
   * projection systematically low (scope Decision 3). Days after `asOf` are ignored.
   */
  days: readonly DailySpend[];
  /** First day of the period, `YYYY-MM-DD`, inclusive. */
  periodStart: string;
  /** Last day of the period, `YYYY-MM-DD`, inclusive. */
  periodEnd: string;
  /** Last settled day. Clamped into the period. Explicit rather than "today" so this stays pure. */
  asOf: string;
  /** Tenant budget for the period, integer micro-USD. Null/undefined when none is configured. */
  budgetMicroUsd?: number | null;
}

/**
 * Project period spend, band it, and say when it crosses budget.
 *
 * The method, in order:
 *
 *   1. Take the trailing settled window (up to TRAILING_WINDOW_DAYS ending at `asOf`) and reduce it
 *      to a median per weekday plus a standard deviation of daily spend.
 *   2. Project each remaining day of the period at its weekday's median, falling back to the
 *      overall median for a weekday never observed.
 *   3. Band the cumulative projection: half-width grows with the square root of the days ahead and
 *      with how much of the period is still unwritten, so it is wide early and pinches shut at the
 *      close.
 *   4. Walk the cumulative path day by day and report the first day it crosses the budget.
 *
 * Below MIN_HISTORY_DAYS settled days it refuses: every projected field comes back null and the
 * breach outcome is `cannot_project`, which the caller must not collapse into `never`.
 *
 * Throws only on input that would silently produce a wrong number: malformed dates, a period that
 * ends before it starts, duplicate days, and non-integer money.
 */
export function forecastSpend(input: ForecastInput): SpendForecast {
  const periodStart = parseDay(input.periodStart, "periodStart");
  const periodEnd = parseDay(input.periodEnd, "periodEnd");
  if (periodEnd < periodStart) {
    throw new RangeError(
      `forecastSpend: periodEnd ${input.periodEnd} is before periodStart ${input.periodStart}`,
    );
  }
  const asOfRaw = parseDay(input.asOf, "asOf");
  // An `asOf` past the end means the period has closed: everything is measured, nothing is left to
  // project. An `asOf` before the start means nothing has been measured yet.
  const asOf = Math.min(asOfRaw, periodEnd);

  const budget = input.budgetMicroUsd ?? null;
  if (budget !== null) assertMicroUsd(budget, "budgetMicroUsd");

  const seen = new Set<string>();
  const settled: { day: number; microUsd: number }[] = [];
  for (const d of input.days) {
    assertMicroUsd(d.microUsd, `microUsd for ${d.date}`);
    if (seen.has(d.date)) throw new Error(`forecastSpend: duplicate day ${d.date}`);
    seen.add(d.date);
    const day = parseDay(d.date, "day");
    if (day <= asOfRaw) settled.push({ day, microUsd: d.microUsd });
  }
  settled.sort((a, b) => a.day - b.day);

  const daysInPeriod = periodEnd - periodStart + 1;
  const daysElapsed = asOf < periodStart ? 0 : asOf - periodStart + 1;
  const daysRemaining = daysInPeriod - daysElapsed;

  const inPeriod = settled.filter((s) => s.day >= periodStart && s.day <= asOf);
  const spendToDate = inPeriod.reduce((sum, s) => sum + s.microUsd, 0);

  const historyDays = settled.length;

  // The refusal. Everything projected is null, and `cannot_project` is a different answer from
  // "never breaches" so a caller cannot mistake silence for safety.
  if (historyDays < MIN_HISTORY_DAYS) {
    return {
      status: "insufficient_history",
      projectedMicroUsd: null,
      lowMicroUsd: null,
      highMicroUsd: null,
      naiveRunRateMicroUsd: null,
      spendToDateMicroUsd: spendToDate,
      daysElapsed,
      daysInPeriod,
      daysRemaining,
      historyDays,
      windowStart: null,
      windowEnd: null,
      weekdayMedianMicroUsd: emptyWeekdayProfile(),
      dailyStdDevMicroUsd: null,
      breach: {
        outcome: "cannot_project",
        date: null,
        dayIndex: null,
        earliestDate: null,
        budgetMicroUsd: budget,
      },
      burndown: [],
    };
  }

  const window = settled.slice(-TRAILING_WINDOW_DAYS);
  const windowValues = window.map((s) => s.microUsd);

  // Median per weekday, not mean: one spike day should move its own weekday a little, not drag the
  // whole projection. Null for a weekday with no observation in the window.
  const buckets: number[][] = Array.from({ length: 7 }, () => []);
  for (const s of window) buckets[weekdayOf(s.day)].push(s.microUsd);
  const weekdayMedian = buckets.map((b) => (b.length > 0 ? median(b) : null));
  const overallMedian = median(windowValues);

  const stdDev = Math.round(sampleStdDev(windowValues));

  // Cumulative actuals for the elapsed days, then the projected tail.
  const byDay = new Map<number, number>();
  for (const s of inPeriod) byDay.set(s.day, s.microUsd);

  const burndown: BurndownPoint[] = [];
  let cumulative = 0;
  for (let day = periodStart; day <= periodEnd; day += 1) {
    if (day <= asOf) {
      // A period day with no row is a genuine zero-spend day, not a gap: the caller passes settled
      // days, so an absent settled day inside the period spent nothing.
      cumulative += byDay.get(day) ?? 0;
      burndown.push({
        date: formatDay(day),
        actualMicroUsd: cumulative,
        projectedMicroUsd: cumulative,
        lowMicroUsd: cumulative,
        highMicroUsd: cumulative,
      });
      continue;
    }
    const expected = weekdayMedian[weekdayOf(day)] ?? overallMedian;
    cumulative += expected;
    const daysAhead = day - Math.max(asOf, periodStart - 1);
    const half = bandHalfWidth(stdDev, daysAhead, daysInPeriod);
    const point = Math.round(cumulative);
    burndown.push({
      date: formatDay(day),
      actualMicroUsd: null,
      projectedMicroUsd: point,
      // Money already spent cannot un-spend, so the low edge never dips below spend to date.
      lowMicroUsd: Math.min(point, Math.max(point - half, spendToDate)),
      highMicroUsd: point + half,
    });
  }

  const last = burndown[burndown.length - 1];
  const projected = last ? last.projectedMicroUsd : spendToDate;
  const low = last ? last.lowMicroUsd : spendToDate;
  const high = last ? last.highMicroUsd : spendToDate;

  const naive =
    daysElapsed > 0 ? Math.round((spendToDate / daysElapsed) * daysInPeriod) : null;

  return {
    status: "ok",
    projectedMicroUsd: projected,
    lowMicroUsd: low,
    highMicroUsd: high,
    naiveRunRateMicroUsd: naive,
    spendToDateMicroUsd: spendToDate,
    daysElapsed,
    daysInPeriod,
    daysRemaining,
    historyDays,
    windowStart: formatDay(window[0].day),
    windowEnd: formatDay(window[window.length - 1].day),
    weekdayMedianMicroUsd: weekdayMedian,
    dailyStdDevMicroUsd: stdDev,
    breach: breachFrom(burndown, budget, periodStart),
    burndown,
  };
}

/**
 * Band half-width `daysAhead` days past the last settled day.
 *
 * `sqrt(daysAhead)` is the variance of a sum of that many independent days; the second factor adds
 * width in proportion to how much of the period is still unwritten, which is the part that makes a
 * day-2 cone visibly wider than a day-25 one. Both factors shrink as the period elapses, so for a
 * given tenant the end-of-period band narrows monotonically day over day, reaching zero on the day
 * the period closes and there is nothing left to guess at.
 */
function bandHalfWidth(stdDev: number, daysAhead: number, daysInPeriod: number): number {
  if (daysAhead <= 0 || stdDev <= 0) return 0;
  const unwritten = daysInPeriod > 0 ? daysAhead / daysInPeriod : 0;
  return Math.round(
    CONFIDENCE_Z * stdDev * Math.sqrt(daysAhead) * (1 + EARLY_PERIOD_INFLATION * unwritten),
  );
}

/**
 * First day the cumulative path crosses the budget. Walks the elapsed days too, so a tenant already
 * over budget gets the day it actually happened rather than a null that reads as "fine".
 */
function breachFrom(
  burndown: readonly BurndownPoint[],
  budget: number | null,
  periodStart: number,
): BreachForecast {
  if (budget === null) {
    return {
      outcome: "no_budget",
      date: null,
      dayIndex: null,
      earliestDate: null,
      budgetMicroUsd: null,
    };
  }

  let date: string | null = null;
  let dayIndex: number | null = null;
  let earliestDate: string | null = null;
  for (const p of burndown) {
    if (earliestDate === null && p.highMicroUsd >= budget) earliestDate = p.date;
    if (p.projectedMicroUsd >= budget) {
      date = p.date;
      dayIndex = parseDay(p.date, "burndown") - periodStart + 1;
      break;
    }
  }

  return {
    outcome: date === null ? "never" : "breaches",
    date,
    dayIndex,
    earliestDate,
    budgetMicroUsd: budget,
  };
}

function emptyWeekdayProfile(): (number | null)[] {
  return [null, null, null, null, null, null, null];
}

/** Median of a non-empty list, rounded to integer micro-USD. Even counts average the two middles. */
function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid];
  return Math.round((sorted[mid - 1] + sorted[mid]) / 2);
}

/** Sample standard deviation. Zero for fewer than two points: no spread is observable from one day. */
function sampleStdDev(values: readonly number[]): number {
  if (values.length < 2) return 0;
  const mean = values.reduce((sum, v) => sum + v, 0) / values.length;
  const variance =
    values.reduce((sum, v) => sum + (v - mean) * (v - mean), 0) / (values.length - 1);
  return Math.sqrt(variance);
}

const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const MS_PER_DAY = 86_400_000;

/** `YYYY-MM-DD` to a UTC day number. UTC throughout so the result never depends on the runner's TZ. */
function parseDay(value: string, label: string): number {
  if (!DAY_PATTERN.test(value)) {
    throw new TypeError(`forecastSpend: ${label} must be YYYY-MM-DD, got ${value}`);
  }
  const ms = Date.parse(`${value}T00:00:00Z`);
  if (Number.isNaN(ms)) {
    throw new RangeError(`forecastSpend: ${label} is not a real date, got ${value}`);
  }
  return Math.round(ms / MS_PER_DAY);
}

function formatDay(day: number): string {
  return new Date(day * MS_PER_DAY).toISOString().slice(0, 10);
}

/** 0 = Sunday .. 6 = Saturday. 1970-01-01 was a Thursday (4). */
function weekdayOf(day: number): number {
  return (((day + 4) % 7) + 7) % 7;
}

function assertMicroUsd(value: number, label: string): void {
  if (!Number.isSafeInteger(value)) {
    // Money is integer micro-USD everywhere in this codebase (see `allocation.ts`). Accepting a
    // float here would put fractional micro-USD into a chart and a breach date, silently.
    throw new TypeError(`forecastSpend: ${label} must be a safe integer micro-USD, got ${value}`);
  }
}
