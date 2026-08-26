// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  budgetVsActual,
  daysBetweenInclusive,
  endOfMonth,
  periodProgress,
  selectBudget,
  type SettledSeriesLike,
  type TenantBudget,
} from "./budgetVsActual";
import { LAYERS, type Layer } from "./cost";
import type { SpendByLayer } from "./types";

const USD = 1_000_000;

function layers(over: Partial<SpendByLayer> = {}): SpendByLayer {
  return { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0, ...over };
}

function sumLayers(byLayer: SpendByLayer): number {
  return LAYERS.reduce((s, l) => s + byLayer[l], 0);
}

interface DaySpec {
  date: string;
  byLayer: SpendByLayer;
  inPeriod?: boolean;
  settled?: boolean;
  inProgress?: boolean;
  awaitingLayers?: Layer[];
}

/**
 * Build a `SettledSpendSeries`-shaped input, with the period totals DERIVED from the days rather
 * than stated separately. Hand-written totals would let a test assert a consistency the production
 * query is not actually giving us.
 */
function series(spec: {
  periodStart: string;
  windowEnd: string;
  days: DaySpec[];
  rule?: "connector-landing" | "day-complete";
  connectorLayers?: string[];
}): SettledSeriesLike {
  const days = spec.days.map((d) => ({
    date: d.date,
    byLayer: d.byLayer,
    totalMicroUsd: sumLayers(d.byLayer),
    inPeriod: d.inPeriod ?? d.date >= spec.periodStart,
    settled: d.settled ?? true,
    inProgress: d.inProgress ?? false,
    awaitingLayers: d.awaitingLayers ?? [],
  }));
  const totals = (keep: (d: (typeof days)[number]) => boolean) => {
    const byLayer = layers();
    let total = 0;
    let dayCount = 0;
    for (const d of days.filter(keep)) {
      dayCount += 1;
      total += d.totalMicroUsd;
      for (const l of LAYERS) byLayer[l] += d.byLayer[l];
    }
    return { dayCount, byLayer, totalMicroUsd: total };
  };
  const settledInPeriod = days.filter((d) => d.inPeriod && d.settled);
  return {
    periodStart: spec.periodStart,
    windowEnd: spec.windowEnd,
    settledThrough: settledInPeriod.at(-1)?.date ?? null,
    rule: spec.rule ?? "connector-landing",
    connectorLayers: spec.connectorLayers ?? ["compute", "egress"],
    days,
    periodSettled: totals((d) => d.inPeriod && d.settled),
    periodObserved: totals((d) => d.inPeriod),
  };
}

function monthBudget(over: Partial<TenantBudget> = {}): TenantBudget {
  return {
    budget_id: "tenant-month",
    period: "month",
    amount_micro: 1000 * USD,
    scope_kind: "tenant",
    scope_value: "",
    starts_on: "2026-01-01",
    ends_on: null,
    ...over,
  };
}

/** Three settled days plus today in progress, one of them still awaiting a connector. */
function demoSeries(): SettledSeriesLike {
  return series({
    periodStart: "2026-08-01",
    windowEnd: "2026-08-05",
    days: [
      { date: "2026-08-01", byLayer: layers({ llm: 100 * USD, compute: 50 * USD }) },
      { date: "2026-08-02", byLayer: layers({ llm: 200 * USD, compute: 100 * USD }) },
      { date: "2026-08-03", byLayer: layers({ llm: 150 * USD, compute: 100 * USD }) },
      {
        date: "2026-08-04",
        byLayer: layers({ llm: 90 * USD }),
        settled: false,
        awaitingLayers: ["compute"],
      },
      {
        date: "2026-08-05",
        byLayer: layers({ llm: 40 * USD }),
        settled: false,
        inProgress: true,
      },
    ],
  });
}

describe("date helpers", () => {
  it("counts inclusive days and finds the end of the month, leap year included", () => {
    expect(daysBetweenInclusive("2026-08-01", "2026-08-01")).toBe(1);
    expect(daysBetweenInclusive("2026-08-01", "2026-08-26")).toBe(26);
    expect(endOfMonth("2026-08-01")).toBe("2026-08-31");
    expect(endOfMonth("2026-02-01")).toBe("2026-02-28");
    expect(endOfMonth("2028-02-10")).toBe("2028-02-29");
  });

  it("counts today as the day in progress and clamps at the period end", () => {
    const p = periodProgress("2026-08-01", "2026-08-26");
    expect(p).toMatchObject({ end: "2026-08-31", daysInPeriod: 31, daysElapsed: 26 });
    expect(p.elapsedFraction).toBeCloseTo(26 / 31);
    expect(periodProgress("2026-08-01", "2026-08-31").elapsedFraction).toBe(1);
  });
});

describe("selectBudget", () => {
  const onDate = "2026-08-26";

  it("finds the monthly tenant-wide budget covering the date", () => {
    const b = monthBudget();
    expect(selectBudget([b], "tenant", "", onDate)).toBe(b);
  });

  it("ignores budgets for another period, scope or date range", () => {
    const cases: TenantBudget[] = [
      // A quarterly budget's dollars would be compared against a month of spend, reporting every
      // tenant as comfortably under.
      monthBudget({ budget_id: "q", period: "quarter" }),
      monthBudget({ budget_id: "f", scope_kind: "feature", scope_value: "research-agent" }),
      monthBudget({ budget_id: "future", starts_on: "2026-09-01" }),
      monthBudget({ budget_id: "closed", ends_on: "2026-07-31" }),
    ];
    for (const b of cases) expect(selectBudget([b], "tenant", "", onDate)).toBeNull();
  });

  it("includes the boundary days of the range", () => {
    const b = monthBudget({ starts_on: onDate, ends_on: onDate });
    expect(selectBudget([b], "tenant", "", onDate)).toBe(b);
  });

  it("is deterministic if the no-overlap invariant is ever broken", () => {
    // CTO-205's EXCLUDE constraint should make this impossible; the point is that a bad backfill
    // produces one stable answer rather than a section that flickers between two budgets.
    const older = monthBudget({ budget_id: "a", starts_on: "2026-01-01" });
    const newer = monthBudget({ budget_id: "b", starts_on: "2026-08-01" });
    expect(selectBudget([older, newer], "tenant", "", onDate)).toBe(newer);
    expect(selectBudget([newer, older], "tenant", "", onDate)).toBe(newer);
  });
});

describe("budgetVsActual", () => {
  it("sums the actual over settled days only and says which ones", () => {
    const c = budgetVsActual(demoSeries(), []);
    expect(c.actualMicroUsd).toBe(700 * USD);
    expect(c.observedMicroUsd).toBe(830 * USD);
    expect(c.coverage).toMatchObject({
      from: "2026-08-01",
      through: "2026-08-03",
      dayCount: 3,
      contiguous: true,
      rule: "connector-landing",
    });
  });

  it("quantifies the excluded days rather than only mentioning them", () => {
    const c = budgetVsActual(demoSeries(), []);
    expect(c.excluded.days.map((d) => d.date)).toEqual(["2026-08-04", "2026-08-05"]);
    expect(c.excluded.microUsd).toBe(130 * USD);
    expect(c.excluded.shareOfObserved).toBeCloseTo(130 / 830);
    // The gap between settled and observed is what stops this figure contradicting the 30-day
    // headline on the same page, so it has to be a real number, not a caveat.
    expect(c.actualMicroUsd + c.excluded.microUsd).toBe(c.observedMicroUsd);
    expect(c.excluded.days[0].awaitingLayers).toEqual(["compute"]);
    expect(c.excluded.days[1].inProgress).toBe(true);
  });

  it("flags a withheld day inside the counted range as non-contiguous", () => {
    const s = series({
      periodStart: "2026-08-01",
      windowEnd: "2026-08-03",
      days: [
        { date: "2026-08-01", byLayer: layers({ llm: 10 * USD }) },
        { date: "2026-08-02", byLayer: layers({ llm: 10 * USD }), settled: false },
        { date: "2026-08-03", byLayer: layers({ llm: 10 * USD }) },
      ],
    });
    expect(budgetVsActual(s, []).coverage).toMatchObject({
      from: "2026-08-01",
      through: "2026-08-03",
      dayCount: 2,
      contiguous: false,
    });
  });

  it("reports no variance at all when no budget is set, never a variance against zero", () => {
    const c = budgetVsActual(demoSeries(), []);
    expect(c.budget).toBeNull();
    expect(c.varianceMicroUsd).toBeNull();
    expect(c.variancePct).toBeNull();
    expect(c.consumedFraction).toBeNull();
    // The reason is what the UI puts in <Blank reason>, so it has to be a real sentence.
    expect(c.noBudgetReason).toMatch(/no monthly tenant-wide budget/);
    // The actual still renders: the spend is known even when the intent is not.
    expect(c.actualMicroUsd).toBe(700 * USD);
    for (const l of c.layers) expect(l.shareOfTenantBudget).toBeNull();
  });

  it("ignores a feature-scoped budget for the tenant headline", () => {
    const featureOnly = monthBudget({ scope_kind: "feature", scope_value: "research-agent" });
    expect(budgetVsActual(demoSeries(), [featureOnly]).budget).toBeNull();
  });

  it("reports a positive variance when over budget", () => {
    const c = budgetVsActual(demoSeries(), [monthBudget({ amount_micro: 500 * USD })]);
    expect(c.budget).toMatchObject({ budgetId: "tenant-month", amountMicroUsd: 500 * USD });
    expect(c.varianceMicroUsd).toBe(200 * USD);
    expect(c.variancePct).toBeCloseTo(0.4);
    expect(c.consumedFraction).toBeCloseTo(1.4);
  });

  it("reports a negative variance when under budget", () => {
    const c = budgetVsActual(demoSeries(), [monthBudget({ amount_micro: 1000 * USD })]);
    expect(c.varianceMicroUsd).toBe(-300 * USD);
    expect(c.variancePct).toBeCloseTo(-0.3);
  });

  it("compares a stored zero budget in dollars but not in percent", () => {
    // A stored zero is a real claim ("this scope may spend nothing"), unlike an absent row. The
    // dollar variance is meaningful; the percentage is undefined rather than infinite.
    const c = budgetVsActual(demoSeries(), [monthBudget({ amount_micro: 0 })]);
    expect(c.budget?.amountMicroUsd).toBe(0);
    expect(c.varianceMicroUsd).toBe(700 * USD);
    expect(c.variancePct).toBeNull();
    expect(c.consumedFraction).toBeNull();
  });

  it("flags a budget that started after the period did", () => {
    const mid = monthBudget({ starts_on: "2026-08-03" });
    expect(budgetVsActual(demoSeries(), [mid]).budget?.coversPeriodToDate).toBe(false);
    expect(budgetVsActual(demoSeries(), [monthBudget()]).budget?.coversPeriodToDate).toBe(true);
  });

  it("splits by layer, ordered by spend, so the variance is attributable", () => {
    const c = budgetVsActual(demoSeries(), [monthBudget({ amount_micro: 500 * USD })]);
    expect(c.layers.map((l) => l.layer)).toEqual([
      "llm",
      "compute",
      "vector",
      "tools",
      "embeddings",
      "egress",
    ]);
    const [llm, compute] = c.layers;
    expect(llm.actualMicroUsd).toBe(450 * USD);
    expect(compute.actualMicroUsd).toBe(250 * USD);
    expect(compute.shareOfActual).toBeCloseTo(250 / 700);
    // "Half the budget went on compute" is the actionable half of this section.
    expect(compute.shareOfTenantBudget).toBeCloseTo(0.5);
    // The layer split adds up to the headline actual.
    expect(c.layers.reduce((s, l) => s + l.actualMicroUsd, 0)).toBe(c.actualMicroUsd);
    // No layer-scoped budget configured: blanks, not zeros.
    expect(c.layers.every((l) => l.budgetMicroUsd === null && l.varianceMicroUsd === null)).toBe(
      true,
    );
  });

  it("uses a layer-scoped budget when one is configured", () => {
    const budgets = [
      monthBudget({ amount_micro: 500 * USD }),
      monthBudget({
        budget_id: "compute-month",
        scope_kind: "layer",
        scope_value: "compute",
        amount_micro: 200 * USD,
      }),
    ];
    const c = budgetVsActual(demoSeries(), budgets);
    const compute = c.layers.find((l) => l.layer === "compute");
    expect(compute?.budgetMicroUsd).toBe(200 * USD);
    expect(compute?.varianceMicroUsd).toBe(50 * USD);
    expect(compute?.variancePct).toBeCloseTo(0.25);
    // Other layers stay blank rather than inheriting the tenant budget.
    expect(c.layers.find((l) => l.layer === "llm")?.budgetMicroUsd).toBeNull();
  });

  it("holds up when nothing has settled yet", () => {
    const s = series({
      periodStart: "2026-08-01",
      windowEnd: "2026-08-01",
      days: [
        {
          date: "2026-08-01",
          byLayer: layers({ llm: 5 * USD }),
          settled: false,
          inProgress: true,
        },
      ],
    });
    const c = budgetVsActual(s, [monthBudget({ amount_micro: 500 * USD })]);
    expect(c.actualMicroUsd).toBe(0);
    expect(c.coverage).toMatchObject({ from: null, through: null, dayCount: 0, contiguous: true });
    // A zero actual against a real budget is a real variance: the tenant has spent nothing that
    // has settled. What must NOT happen is a share-of-total computed by dividing by zero.
    expect(c.varianceMicroUsd).toBe(-500 * USD);
    expect(c.layers.every((l) => l.shareOfActual === null)).toBe(true);
    expect(c.excluded.microUsd).toBe(5 * USD);
  });

  it("does not project: the variance is actual minus the full budget, never pro-rated", () => {
    // Day 3 of a 31-day month, 700 spent against a 1000 budget. A pro-rated comparison would call
    // this wildly over; the measured one is 300 under, and the elapsed fraction is what tells the
    // reader how little that means yet. CTO-210 owns the projection.
    const c = budgetVsActual(demoSeries(), [monthBudget({ amount_micro: 1000 * USD })]);
    expect(c.varianceMicroUsd).toBe(700 * USD - 1000 * USD);
    expect(c.period.daysElapsed).toBe(5);
    expect(c.period.daysInPeriod).toBe(31);
  });
});
