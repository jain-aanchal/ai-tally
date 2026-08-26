// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  MIN_HISTORY_DAYS,
  type DailySpend,
  forecastSpend,
} from "./forecast";

const MS_PER_DAY = 86_400_000;

function addDays(date: string, n: number): string {
  return new Date(Date.parse(`${date}T00:00:00Z`) + n * MS_PER_DAY).toISOString().slice(0, 10);
}

/** 0 = Sunday .. 6 = Saturday, matching the engine. */
function dow(date: string): number {
  return new Date(Date.parse(`${date}T00:00:00Z`)).getUTCDay();
}

/** Build `count` consecutive days starting at `start`, amounts from `amount(date, dow, index)`. */
function series(
  start: string,
  count: number,
  amount: (date: string, weekday: number, index: number) => number,
): DailySpend[] {
  const out: DailySpend[] = [];
  for (let i = 0; i < count; i += 1) {
    const date = addDays(start, i);
    out.push({ date, microUsd: Math.round(amount(date, dow(date), i)) });
  }
  return out;
}

/**
 * Deterministic LCG. The synthetic month has to be reproducible or the "weighted beats naive"
 * assertion becomes a coin flip that fails in CI once a quarter.
 */
function seededJitter(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 4_294_967_296; // [0, 1)
  };
}

// March 2024: 31 days, starts on a Friday. Enough weekend/weekday interleaving that a flat run-rate
// computed over a part-month lands visibly wrong.
const PERIOD_START = "2024-03-01";
const PERIOD_END = "2024-03-31";
/** 28 settled days of trailing history immediately before the period. */
const HISTORY_START = addDays(PERIOD_START, -28);

const WEEKDAY_MICRO = 1_000_000;
const WEEKEND_MICRO = 300_000; // a B2B product at roughly a third of weekday volume on a Sunday

function weekendHeavy(jitterSeed: number | null): (d: string, w: number) => number {
  const rand = jitterSeed === null ? null : seededJitter(jitterSeed);
  return (_date, weekday) => {
    const base = weekday === 0 || weekday === 6 ? WEEKEND_MICRO : WEEKDAY_MICRO;
    // +/-10 percent of seeded noise, so the medians are estimates rather than an exact replay.
    return rand ? base * (0.9 + 0.2 * rand()) : base;
  };
}

describe("day-of-week weighted projection", () => {
  it("beats the naive run-rate on a seeded weekend-heavy month", () => {
    const shape = weekendHeavy(20_240_301);
    const history = series(HISTORY_START, 28, shape);
    const march = series(PERIOD_START, 31, shape);

    // Truth: what the month actually lands at. Only the first 15 days are visible to the forecast.
    const truth = march.reduce((s, d) => s + d.microUsd, 0);
    const asOf = addDays(PERIOD_START, 14); // 2024-03-15, a Friday

    const f = forecastSpend({
      days: [...history, ...march.slice(0, 15)],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf,
    });

    expect(f.status).toBe("ok");
    expect(f.projectedMicroUsd).not.toBeNull();
    expect(f.naiveRunRateMicroUsd).not.toBeNull();

    const weightedError = Math.abs(f.projectedMicroUsd! - truth);
    const naiveError = Math.abs(f.naiveRunRateMicroUsd! - truth);

    // The two must actually disagree, and the weighted one must be the closer of the two.
    expect(f.projectedMicroUsd).not.toBe(f.naiveRunRateMicroUsd);
    expect(weightedError).toBeLessThan(naiveError);
    // Weighted lands within 3 percent of truth; naive is off by more than that.
    expect(weightedError / truth).toBeLessThan(0.03);
    expect(naiveError / truth).toBeGreaterThan(0.03);
  });

  it("reproduces a clean periodic month exactly", () => {
    // No jitter: the weekday medians ARE the generating pattern, so the projection should land on
    // the true total to the micro-USD. Any drift here is a bug in the weekday walk, not noise.
    const shape = weekendHeavy(null);
    const history = series(HISTORY_START, 28, shape);
    const march = series(PERIOD_START, 31, shape);
    const truth = march.reduce((s, d) => s + d.microUsd, 0);

    const f = forecastSpend({
      days: [...history, ...march.slice(0, 10)],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
    });

    expect(f.projectedMicroUsd).toBe(truth);
    // Sanity line still disagrees, which is the whole reason it is labelled as one.
    expect(f.naiveRunRateMicroUsd).not.toBe(truth);
  });

  it("uses a median per weekday, so one spike day does not move the projection much", () => {
    const flat = series(HISTORY_START, 28, () => WEEKDAY_MICRO);
    const spiked = flat.map((d, i) =>
      i === flat.length - 1 ? { ...d, microUsd: WEEKDAY_MICRO * 50 } : d,
    );
    const base = { periodStart: PERIOD_START, periodEnd: PERIOD_END, asOf: PERIOD_START };

    const clean = forecastSpend({ ...base, days: flat });
    const withSpike = forecastSpend({ ...base, days: spiked });

    // A mean over 28 days would drag the projection up by ~75 percent. The median moves one
    // weekday's estimate by nothing at all here, because 3 of its 4 observations are unchanged.
    expect(withSpike.projectedMicroUsd).toBe(clean.projectedMicroUsd);
  });

  it("reports the trailing window it computed from", () => {
    const days = series(HISTORY_START, 40, () => WEEKDAY_MICRO);
    const asOf = days[days.length - 1].date;
    const f = forecastSpend({ days, periodStart: PERIOD_START, periodEnd: PERIOD_END, asOf });

    expect(f.historyDays).toBe(40);
    expect(f.windowEnd).toBe(asOf);
    expect(f.windowStart).toBe(addDays(asOf, -27));
  });

  it("leaves a never-observed weekday null and falls back to the overall median", () => {
    // A caller that only has settled weekdays: Saturday and Sunday were never seen at all.
    const days = series(HISTORY_START, 28, () => WEEKDAY_MICRO).filter(
      (d) => dow(d.date) !== 0 && dow(d.date) !== 6,
    );
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: days[days.length - 1].date,
    });

    expect(f.weekdayMedianMicroUsd[0]).toBeNull(); // Sunday: unknown, not zero
    expect(f.weekdayMedianMicroUsd[6]).toBeNull(); // Saturday
    expect(f.weekdayMedianMicroUsd[3]).toBe(WEEKDAY_MICRO);
    // Fallback is the overall median, so every remaining day projects at the weekday level.
    expect(f.projectedMicroUsd).toBe(31 * WEEKDAY_MICRO);
  });
});

describe("the confidence band", () => {
  it("narrows monotonically as the period elapses, and closes to nothing at period end", () => {
    // A strictly periodic series: the trailing 28-day window is the same multiset of values on
    // every `asOf`, so the standard deviation is constant and any change in width comes from time
    // elapsed rather than from the data moving underneath the test.
    const shape = weekendHeavy(null);
    const history = series(HISTORY_START, 28, shape);
    const march = series(PERIOD_START, 31, shape);

    const widths: number[] = [];
    for (let elapsed = 1; elapsed <= 31; elapsed += 1) {
      const f = forecastSpend({
        days: [...history, ...march.slice(0, elapsed)],
        periodStart: PERIOD_START,
        periodEnd: PERIOD_END,
        asOf: addDays(PERIOD_START, elapsed - 1),
      });
      expect(f.status).toBe("ok");
      widths.push(f.highMicroUsd! - f.lowMicroUsd!);
    }

    for (let i = 1; i < widths.length; i += 1) {
      expect(widths[i]).toBeLessThanOrEqual(widths[i - 1]);
    }
    // Day 2 is a genuinely fat cone; the closed period is a single number.
    expect(widths[1]).toBeGreaterThan(widths[24]);
    expect(widths[widths.length - 1]).toBe(0);
  });

  it("brackets the point projection and never dips below money already spent", () => {
    const shape = weekendHeavy(7);
    const f = forecastSpend({
      days: [...series(HISTORY_START, 28, shape), ...series(PERIOD_START, 12, shape)],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 11),
    });

    expect(f.lowMicroUsd!).toBeLessThanOrEqual(f.projectedMicroUsd!);
    expect(f.projectedMicroUsd!).toBeLessThanOrEqual(f.highMicroUsd!);
    expect(f.lowMicroUsd!).toBeGreaterThanOrEqual(f.spendToDateMicroUsd);
  });

  it("has no band at all when daily spend never varies", () => {
    const days = series(HISTORY_START, 28, () => WEEKDAY_MICRO);
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: days[days.length - 1].date,
    });
    expect(f.dailyStdDevMicroUsd).toBe(0);
    expect(f.lowMicroUsd).toBe(f.projectedMicroUsd);
    expect(f.highMicroUsd).toBe(f.projectedMicroUsd);
  });

  it("widens the cone with distance from the last settled day", () => {
    const shape = weekendHeavy(99);
    const f = forecastSpend({
      days: [...series(HISTORY_START, 28, shape), ...series(PERIOD_START, 5, shape)],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 4),
    });

    const future = f.burndown.filter((p) => p.actualMicroUsd === null);
    const widths = future.map((p) => p.highMicroUsd - p.lowMicroUsd);
    for (let i = 1; i < widths.length; i += 1) {
      expect(widths[i]).toBeGreaterThan(widths[i - 1]);
    }
    // Settled days carry no band: they are measured, not projected.
    for (const p of f.burndown.filter((q) => q.actualMicroUsd !== null)) {
      expect(p.lowMicroUsd).toBe(p.actualMicroUsd);
      expect(p.highMicroUsd).toBe(p.actualMicroUsd);
    }
  });
});

describe("the minimum-history guard", () => {
  it("returns null rather than a number below the floor", () => {
    const days = series(addDays(PERIOD_START, -4), 4, () => WEEKDAY_MICRO);
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 3),
      budgetMicroUsd: 1_000,
    });

    expect(f.status).toBe("insufficient_history");
    expect(f.projectedMicroUsd).toBeNull();
    expect(f.lowMicroUsd).toBeNull();
    expect(f.highMicroUsd).toBeNull();
    expect(f.naiveRunRateMicroUsd).toBeNull();
    expect(f.dailyStdDevMicroUsd).toBeNull();
    expect(f.windowStart).toBeNull();
    expect(f.windowEnd).toBeNull();
    expect(f.burndown).toEqual([]);
    expect(f.weekdayMedianMicroUsd.every((v) => v === null)).toBe(true);
    // Measured facts are still reported: refusing to project is not refusing to count.
    expect(f.historyDays).toBe(4);
    expect(f.daysInPeriod).toBe(31);
  });

  it("flips to a projection exactly at the floor", () => {
    const build = (n: number) =>
      forecastSpend({
        days: series(addDays(PERIOD_START, -n), n, () => WEEKDAY_MICRO),
        periodStart: PERIOD_START,
        periodEnd: PERIOD_END,
        asOf: addDays(PERIOD_START, -1),
      });

    expect(build(MIN_HISTORY_DAYS - 1).status).toBe("insufficient_history");
    expect(build(MIN_HISTORY_DAYS).status).toBe("ok");
    expect(build(MIN_HISTORY_DAYS).projectedMicroUsd).not.toBeNull();
  });

  it("ignores days after asOf when counting history", () => {
    // 30 days on hand but only 10 of them settled: still a refusal, not a projection built from
    // data the caller told us it cannot vouch for yet.
    const f = forecastSpend({
      days: series(addDays(PERIOD_START, -10), 30, () => WEEKDAY_MICRO),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, -1),
    });
    expect(f.historyDays).toBe(10);
    expect(f.status).toBe("insufficient_history");
  });
});

describe("the breach date", () => {
  const flatMarch = (elapsed: number) => [
    ...series(HISTORY_START, 28, () => WEEKDAY_MICRO),
    ...series(PERIOD_START, elapsed, () => WEEKDAY_MICRO),
  ];

  it("is exact on a series constructed to cross on a known day", () => {
    // A flat 1 USD/day: cumulative hits 20 USD on the 20th, and not a day earlier.
    const f = forecastSpend({
      days: flatMarch(10),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
      budgetMicroUsd: 20 * WEEKDAY_MICRO,
    });

    expect(f.breach.outcome).toBe("breaches");
    expect(f.breach.date).toBe("2024-03-20");
    expect(f.breach.dayIndex).toBe(20);
    expect(f.breach.budgetMicroUsd).toBe(20 * WEEKDAY_MICRO);
  });

  it("reports the day it already happened when the period is over budget in the past", () => {
    const f = forecastSpend({
      days: flatMarch(20),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 19),
      budgetMicroUsd: 5 * WEEKDAY_MICRO,
    });

    expect(f.breach.outcome).toBe("breaches");
    expect(f.breach.date).toBe("2024-03-05");
    expect(f.breach.dayIndex).toBe(5);
  });

  it("distinguishes 'never breaches' from 'cannot project'", () => {
    const never = forecastSpend({
      days: flatMarch(10),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
      budgetMicroUsd: 500 * WEEKDAY_MICRO,
    });
    const cannot = forecastSpend({
      days: series(PERIOD_START, 4, () => WEEKDAY_MICRO),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 3),
      budgetMicroUsd: 500 * WEEKDAY_MICRO,
    });

    expect(never.breach.outcome).toBe("never");
    expect(never.breach.date).toBeNull();
    expect(cannot.breach.outcome).toBe("cannot_project");
    expect(cannot.breach.date).toBeNull();
    // Same null date, different meaning, and the caller can tell them apart without guessing.
    expect(never.breach.outcome).not.toBe(cannot.breach.outcome);
    // Both still echo the budget they were asked about.
    expect(cannot.breach.budgetMicroUsd).toBe(500 * WEEKDAY_MICRO);
  });

  it("says no_budget rather than assuming a budget of zero", () => {
    const f = forecastSpend({
      days: flatMarch(10),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
    });
    expect(f.breach.outcome).toBe("no_budget");
    expect(f.breach.date).toBeNull();
    expect(f.breach.budgetMicroUsd).toBeNull();
    // A zero budget is a real budget, and breaches immediately. Not the same answer.
    const zero = forecastSpend({
      days: flatMarch(10),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
      budgetMicroUsd: 0,
    });
    expect(zero.breach.outcome).toBe("breaches");
    expect(zero.breach.date).toBe(PERIOD_START);
  });

  it("puts the bad-case crossing on or before the likely one", () => {
    const shape = weekendHeavy(4_242);
    const f = forecastSpend({
      days: [...series(HISTORY_START, 28, shape), ...series(PERIOD_START, 8, shape)],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 7),
      budgetMicroUsd: 24 * WEEKDAY_MICRO,
    });

    expect(f.breach.outcome).toBe("breaches");
    expect(f.breach.earliestDate).not.toBeNull();
    expect(Date.parse(f.breach.earliestDate!)).toBeLessThanOrEqual(Date.parse(f.breach.date!));
  });
});

describe("period edges and inputs", () => {
  it("projects nothing once the period has closed", () => {
    const days = [
      ...series(HISTORY_START, 28, () => WEEKDAY_MICRO),
      ...series(PERIOD_START, 31, () => WEEKDAY_MICRO),
    ];
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: "2024-04-09", // well past the end; clamped into the period
    });

    expect(f.daysElapsed).toBe(31);
    expect(f.daysRemaining).toBe(0);
    expect(f.spendToDateMicroUsd).toBe(31 * WEEKDAY_MICRO);
    expect(f.projectedMicroUsd).toBe(f.spendToDateMicroUsd);
    expect(f.lowMicroUsd).toBe(f.spendToDateMicroUsd);
    expect(f.highMicroUsd).toBe(f.spendToDateMicroUsd);
  });

  it("projects the whole period when it has not started yet", () => {
    const f = forecastSpend({
      days: series(HISTORY_START, 28, () => WEEKDAY_MICRO),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, -1),
    });

    expect(f.daysElapsed).toBe(0);
    expect(f.daysRemaining).toBe(31);
    expect(f.spendToDateMicroUsd).toBe(0);
    // No elapsed days means no denominator for a run-rate. Null, not Infinity, not zero.
    expect(f.naiveRunRateMicroUsd).toBeNull();
    expect(f.projectedMicroUsd).toBe(31 * WEEKDAY_MICRO);
  });

  it("treats an absent settled day inside the period as a zero-spend day", () => {
    const days = [
      ...series(HISTORY_START, 28, () => WEEKDAY_MICRO),
      ...series(PERIOD_START, 10, () => WEEKDAY_MICRO).filter((d) => d.date !== "2024-03-04"),
    ];
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 9),
    });
    expect(f.spendToDateMicroUsd).toBe(9 * WEEKDAY_MICRO);
    expect(f.burndown[3].actualMicroUsd).toBe(f.burndown[2].actualMicroUsd);
  });

  it("does not care what order the days arrive in", () => {
    const days = series(HISTORY_START, 30, weekendHeavy(11));
    const base = { periodStart: PERIOD_START, periodEnd: PERIOD_END, asOf: addDays(PERIOD_START, 1) };
    expect(forecastSpend({ ...base, days: [...days].reverse() })).toEqual(
      forecastSpend({ ...base, days }),
    );
  });

  it("carries a credit through instead of clamping it away", () => {
    const days = series(HISTORY_START, 28, () => WEEKDAY_MICRO).concat(
      series(PERIOD_START, 3, (_d, _w, i) => (i === 1 ? -5 * WEEKDAY_MICRO : WEEKDAY_MICRO)),
    );
    const f = forecastSpend({
      days,
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 2),
    });
    expect(f.spendToDateMicroUsd).toBe(-3 * WEEKDAY_MICRO);
  });

  it("returns integer micro-USD everywhere", () => {
    const f = forecastSpend({
      days: [...series(HISTORY_START, 28, weekendHeavy(5)), ...series(PERIOD_START, 6, weekendHeavy(6))],
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: addDays(PERIOD_START, 5),
      budgetMicroUsd: 25 * WEEKDAY_MICRO,
    });

    for (const v of [
      f.projectedMicroUsd,
      f.lowMicroUsd,
      f.highMicroUsd,
      f.naiveRunRateMicroUsd,
      f.spendToDateMicroUsd,
      f.dailyStdDevMicroUsd,
    ]) {
      expect(Number.isSafeInteger(v)).toBe(true);
    }
    for (const p of f.burndown) {
      expect(Number.isSafeInteger(p.projectedMicroUsd)).toBe(true);
      expect(Number.isSafeInteger(p.lowMicroUsd)).toBe(true);
      expect(Number.isSafeInteger(p.highMicroUsd)).toBe(true);
    }
    expect(f.burndown).toHaveLength(31);
  });

  it("throws on input that would silently produce a wrong number", () => {
    const good = {
      days: series(HISTORY_START, 28, () => WEEKDAY_MICRO),
      periodStart: PERIOD_START,
      periodEnd: PERIOD_END,
      asOf: PERIOD_START,
    };

    expect(() => forecastSpend({ ...good, periodStart: "03/01/2024" })).toThrow(TypeError);
    expect(() => forecastSpend({ ...good, periodEnd: "2024-02-01" })).toThrow(RangeError);
    expect(() =>
      forecastSpend({ ...good, days: [{ date: "2024-02-01", microUsd: 1.5 }] }),
    ).toThrow(TypeError);
    expect(() =>
      forecastSpend({
        ...good,
        days: [
          { date: "2024-02-01", microUsd: 1 },
          { date: "2024-02-01", microUsd: 2 },
        ],
      }),
    ).toThrow(/duplicate day/);
    expect(() => forecastSpend({ ...good, budgetMicroUsd: 10.5 })).toThrow(TypeError);
  });
});
