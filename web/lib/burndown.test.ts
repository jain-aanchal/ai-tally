// SPDX-License-Identifier: Apache-2.0
// The burn-down view model (CTO-210). Every test here is one of the ticket's honesty requirements,
// because each of them fails SILENTLY if it regresses: a cone drawn from four days looks exactly
// like a cone drawn from forty, and an axis built from the wrong clock looks exactly like one built
// from the right one until you count the days.

import { describe, expect, it } from "vitest";

import { burndownSection } from "./burndown";
import type { SettledSeriesLike, TenantBudget } from "./budgetVsActual";
import { LAYERS } from "./cost";
import { MIN_HISTORY_DAYS } from "./forecast";
import type { SpendByLayer } from "./types";

const USD = 1_000_000;

function layers(over: Partial<SpendByLayer> = {}): SpendByLayer {
  return { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0, ...over };
}

function addDays(iso: string, n: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** One row of `SettledSeriesLike["days"]`, mutable so the fixtures can be built up in a loop. */
type DayRow = {
  date: string;
  byLayer: SpendByLayer;
  totalMicroUsd: number;
  inPeriod: boolean;
  settled: boolean;
  inProgress: boolean;
  awaitingLayers: string[];
};

interface SeriesOptions {
  /** Days of settled history ending the day before `today`. */
  settledDays: number;
  /** Per-day spend in whole dollars, split 70/30 between llm and compute. */
  perDayUsd?: number;
  /** First day of the calendar month. */
  periodStart?: string;
  /** Today per ClickHouse. Always unsettled: it is the day in progress. */
  today?: string;
}

/**
 * A settled series shaped like `querySettledCostSeries`' output: `settledDays` complete days ending
 * yesterday, plus today still accruing and therefore excluded.
 */
function series(opts: SeriesOptions): SettledSeriesLike {
  const periodStart = opts.periodStart ?? "2026-08-01";
  const today = opts.today ?? "2026-08-20";
  const perDay = (opts.perDayUsd ?? 100) * USD;
  const days: DayRow[] = [];
  for (let i = opts.settledDays; i >= 1; i -= 1) {
    const date = addDays(today, -i);
    days.push({
      date,
      byLayer: layers({ llm: Math.round(perDay * 0.7), compute: Math.round(perDay * 0.3) }),
      totalMicroUsd: perDay,
      inPeriod: date >= periodStart,
      settled: true,
      inProgress: false,
      awaitingLayers: [],
    });
  }
  days.push({
    date: today,
    byLayer: layers({ llm: Math.round(perDay * 0.35), compute: Math.round(perDay * 0.15) }),
    totalMicroUsd: Math.round(perDay * 0.5),
    inPeriod: today >= periodStart,
    settled: false,
    inProgress: true,
    awaitingLayers: [],
  });

  const totals = (keep: (d: DayRow) => boolean) => {
    const byLayer = layers();
    let totalMicroUsd = 0;
    let dayCount = 0;
    for (const d of days.filter(keep)) {
      dayCount += 1;
      totalMicroUsd += d.totalMicroUsd;
      for (const l of LAYERS) byLayer[l] += d.byLayer[l];
    }
    return { dayCount, byLayer, totalMicroUsd };
  };

  const settledDates = days.filter((d) => d.settled).map((d) => d.date);
  return {
    periodStart,
    windowEnd: today,
    settledThrough: settledDates[settledDates.length - 1] ?? null,
    rule: "connector-landing",
    connectorLayers: ["compute", "egress"],
    days,
    periodSettled: totals((d) => d.inPeriod && d.settled),
    periodObserved: totals((d) => d.inPeriod),
  };
}

function budget(amountUsd: number, over: Partial<TenantBudget> = {}): TenantBudget {
  return {
    budget_id: "tenant-month",
    period: "month",
    amount_micro: amountUsd * USD,
    scope_kind: "tenant",
    scope_value: "",
    starts_on: "2026-01-01",
    ends_on: null,
    ...over,
  };
}

describe("burndownSection", () => {
  it("refuses to project below the minimum history, and says so as `cannot_project`", () => {
    // Four days is the scope doc's own example of a forecast that must not ship.
    const s = burndownSection(series({ settledDays: 4 }), [budget(1_000)]);
    expect(s.forecast.status).toBe("insufficient_history");
    expect(s.forecast.projectedMicroUsd).toBeNull();
    // The cone must be EMPTY, not merely small: the card renders no chart off this.
    expect(s.forecast.burndown).toEqual([]);
    expect(s.forecast.breach.outcome).toBe("cannot_project");
    expect(s.forecast.breach.date).toBeNull();
    // The refusal still has to say how far off it is.
    expect(s.window.dayCount).toBe(4);
    expect(s.window.requiredDays).toBe(MIN_HISTORY_DAYS);
  });

  it("projects at exactly the minimum history and not one day below it", () => {
    const below = burndownSection(series({ settledDays: MIN_HISTORY_DAYS - 1 }), []);
    const at = burndownSection(series({ settledDays: MIN_HISTORY_DAYS }), []);
    expect(below.forecast.status).toBe("insufficient_history");
    expect(at.forecast.status).toBe("ok");
    expect(at.forecast.burndown.length).toBeGreaterThan(0);
  });

  it("states the input window it actually used, and excludes the in-progress day", () => {
    const s = burndownSection(series({ settledDays: 20, today: "2026-08-20" }), []);
    expect(s.window.through).toBe("2026-08-19");
    expect(s.window.dayCount).toBe(20);
    expect(s.window.contiguous).toBe(true);
    expect(s.window.rule).toBe("connector-landing");
    expect(s.window.waitsOn).toEqual(["compute", "egress"]);
    // Today is inside the period, unsettled, and carries half a day of spend.
    expect(s.window.excludedDays).toEqual(["2026-08-20"]);
    expect(s.window.excludedMicroUsd).toBe(50 * USD);
    expect(s.window.excludedShareOfObserved).toBeGreaterThan(0);
    // And the engine agrees about the window it was fed.
    expect(s.forecast.windowEnd).toBe("2026-08-19");
  });

  it("reports a breach date on the day the projection crosses the budget", () => {
    // 100/day settled through the 19th is 1,900 so far; a 2,400 budget is crossed a few days later.
    const s = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), [budget(2_400)]);
    expect(s.forecast.breach.outcome).toBe("breaches");
    expect(s.forecast.breach.date).toBe("2026-08-24");
    expect(s.forecast.breach.dayIndex).toBe(24);
    expect(s.varianceMicroUsd).toBeGreaterThan(0);
    expect(s.variancePct).toBeGreaterThan(0);
    // The breach day must exist on the axis the chart draws, or the marker lands nowhere.
    expect(s.forecast.burndown.map((p) => p.date)).toContain("2026-08-24");
  });

  it("distinguishes `never` from `cannot_project`: one projected, the other did not", () => {
    const never = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), [budget(100_000)]);
    expect(never.forecast.breach.outcome).toBe("never");
    expect(never.forecast.projectedMicroUsd).not.toBeNull();
    expect(never.varianceMicroUsd).toBeLessThan(0);

    const cannot = burndownSection(series({ settledDays: 3 }), [budget(100_000)]);
    expect(cannot.forecast.breach.outcome).toBe("cannot_project");
    expect(cannot.forecast.projectedMicroUsd).toBeNull();
    // Not a variance of zero, and not a reassuring negative one: no claim at all.
    expect(cannot.varianceMicroUsd).toBeNull();
  });

  it("renders no budget as its own outcome, never as a budget of zero", () => {
    const s = burndownSection(series({ settledDays: 20 }), []);
    expect(s.forecast.breach.outcome).toBe("no_budget");
    expect(s.budget).toBeNull();
    expect(s.varianceMicroUsd).toBeNull();
    expect(s.variancePct).toBeNull();
    expect(s.noBudgetReason).toContain("no monthly tenant-wide budget");
    // A zero budget would have made this "breaches on day 1", which is the trap.
    expect(s.forecast.breach.date).toBeNull();
  });

  it("keeps the naive run-rate as a separate labelled line rather than the headline", () => {
    const s = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), []);
    expect(s.forecast.naiveRunRateMicroUsd).not.toBeNull();
    expect(s.forecast.projectedMicroUsd).not.toBeNull();
  });

  it("projects every layer independently and reports the sum against the headline", () => {
    const s = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), []);
    expect(s.layers).toHaveLength(LAYERS.length);
    // 70/30 llm/compute, so llm leads and is "the reason".
    expect(s.largestLayer).toBe("llm");
    expect(s.layers[0].layer).toBe("llm");
    expect(s.layers[0].shareOfProjected).toBeCloseTo(0.7, 2);
    // The sum is computed and exposed whether or not it matches, so the card can print the gap.
    expect(s.layerSumMicroUsd).not.toBeNull();
    const gap = Math.abs((s.layerSumMicroUsd ?? 0) - (s.forecast.projectedMicroUsd ?? 0));
    expect(s.layersReconcile).toBe(gap === 0);
  });

  it("attaches layer-scoped budgets to their own rows only", () => {
    const s = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), [
      budget(5_000),
      budget(1_000, { budget_id: "compute", scope_kind: "layer", scope_value: "compute" }),
    ]);
    const compute = s.layers.find((l) => l.layer === "compute");
    const llm = s.layers.find((l) => l.layer === "llm");
    expect(compute?.budgetMicroUsd).toBe(1_000 * USD);
    expect(compute?.varianceMicroUsd).not.toBeNull();
    expect(llm?.budgetMicroUsd).toBeNull();
    expect(llm?.varianceMicroUsd).toBeNull();
  });

  it("builds the axis from ClickHouse's dates, not from this machine's clock", () => {
    // A month nowhere near today. If any part of this pipeline consulted `Date.now()` the axis
    // would be a different month, which is the CTO-203 failure mode exactly.
    const s = burndownSection(
      series({ settledDays: 20, periodStart: "2031-02-01", today: "2031-02-20" }),
      [],
    );
    expect(s.period.start).toBe("2031-02-01");
    // February 2031 is not a leap year: 28 days, by arithmetic on ClickHouse's own periodStart.
    expect(s.period.end).toBe("2031-02-28");
    expect(s.forecast.burndown).toHaveLength(28);
    expect(s.forecast.burndown[0].date).toBe("2031-02-01");
    expect(s.forecast.burndown[27].date).toBe("2031-02-28");
    expect(s.period.today).toBe("2031-02-20");
  });

  it("checks the chart's own days against the settled figure printed beside them", () => {
    const s = burndownSection(series({ settledDays: 20, perDayUsd: 100 }), []);
    // 19 settled days inside August (the 1st through the 19th) at 100 each.
    expect(s.settledPeriodMicroUsd).toBe(1_900 * USD);
    expect(s.chartActualMicroUsd).toBe(1_900 * USD);
    expect(s.chartReconciles).toBe(true);
  });

  it("still reconciles when a mid-month day is withheld, because it is never plotted", () => {
    const s = series({ settledDays: 20, perDayUsd: 100 });
    // A connector has not landed for the 10th: it drops out of both the baseline and the chart.
    const days = s.days.map((d) =>
      d.date === "2026-08-10" ? { ...d, settled: false, awaitingLayers: ["compute"] } : d,
    );
    const rebuilt: SettledSeriesLike = {
      ...s,
      days,
      settledThrough: "2026-08-19",
      periodSettled: {
        dayCount: 18,
        byLayer: layers({ llm: 18 * 70 * USD, compute: 18 * 30 * USD }),
        totalMicroUsd: 1_800 * USD,
      },
    };
    const section = burndownSection(rebuilt, []);
    expect(section.window.contiguous).toBe(false);
    expect(section.window.excludedDays).toContain("2026-08-10");
    expect(section.chartActualMicroUsd).toBe(1_800 * USD);
    expect(section.chartReconciles).toBe(true);
  });

  it("does not count days before the tenant was ever seen spending as history", () => {
    // The live shape this defends against: `querySettledCostSeries` gives every calendar day in its
    // 30-day window a slot, so a tenant onboarded six days ago arrives with 29 "settled" days, 23 of
    // which are zeros meaning "did not exist yet". Counting those would sail past the history floor
    // on a tenant that has been sending data for less than a week.
    const s = series({ settledDays: 25, perDayUsd: 100 });
    // Settled history runs 2026-07-26 to 2026-08-19 (today, the 20th, is still accruing). Zero
    // everything before the 14th and six settled days are left.
    const firstReal = "2026-08-14";
    const days = s.days.map((d) =>
      d.date < firstReal
        ? { ...d, byLayer: layers(), totalMicroUsd: 0 }
        : d,
    );
    const section = burndownSection({ ...s, days }, []);
    expect(section.window.firstObservedDay).toBe(firstReal);
    expect(section.window.trimmedLeadingDays).toBeGreaterThan(0);
    // Six settled days, below the floor, so no cone.
    expect(section.window.dayCount).toBe(6);
    expect(section.forecast.status).toBe("insufficient_history");
    expect(section.forecast.burndown).toEqual([]);
  });

  it("counts a genuine zero-spend day inside a live tenant's history", () => {
    // The other half of the same rule: once the tenant HAS been seen, a later zero day is real and
    // must stay in the baseline, or a quiet weekend would silently shorten the input window.
    const s = series({ settledDays: 20, perDayUsd: 100 });
    const days = s.days.map((d) =>
      d.date === "2026-08-15" ? { ...d, byLayer: layers(), totalMicroUsd: 0 } : d,
    );
    const section = burndownSection({ ...s, days }, []);
    expect(section.window.trimmedLeadingDays).toBe(0);
    expect(section.window.dayCount).toBe(20);
    expect(section.forecast.status).toBe("ok");
  });

  it("makes no reconciliation claim about a chart it did not draw", () => {
    // Below the floor the burn-down is empty by design, so "the days plotted sum to nothing but
    // month to date is $1.50" would be a bug report about a chart that was never rendered.
    const s = series({ settledDays: 25, perDayUsd: 100 });
    const days = s.days.map((d) =>
      d.date < "2026-08-14" ? { ...d, byLayer: layers(), totalMicroUsd: 0 } : d,
    );
    const section = burndownSection({ ...s, days }, []);
    expect(section.forecast.status).toBe("insufficient_history");
    expect(section.chartActualMicroUsd).toBeNull();
    expect(section.settledPeriodMicroUsd).toBeGreaterThan(0);
    expect(section.chartReconciles).toBe(true);
  });

  it("refuses rather than projecting when nothing has settled at all", () => {
    const s = series({ settledDays: 0 });
    const section = burndownSection(s, [budget(1_000)]);
    expect(section.period.asOf).toBeNull();
    expect(section.forecast.status).toBe("insufficient_history");
    expect(section.chartActualMicroUsd).toBeNull();
    expect(section.chartReconciles).toBe(true);
  });
});

// CTO-211 (F7): the same view model built for a slice rather than the whole bill. The series handed
// in is the SCOPE's own (`querySettledCostSeries(scope)`), so everything above already applies per
// scope; what is tested here is the three things the scope argument itself changes.
describe("burndownSection, scoped (CTO-211)", () => {
  const feature = { kind: "feature" as const, value: "research-agent" };

  it("selects that scope's budget and not the tenant-wide one", () => {
    const budgets = [
      budget(1_000),
      budget(300, { budget_id: "f", scope_kind: "feature", scope_value: "research-agent" }),
    ];
    const scoped = burndownSection(series({ settledDays: 20 }), budgets, feature);
    expect(scoped.scope).toEqual(feature);
    expect(scoped.budget?.budgetId).toBe("f");
    expect(scoped.budget?.amountMicroUsd).toBe(300 * USD);
    // Default is unchanged: still the tenant-wide row, exactly what F6 shipped.
    expect(burndownSection(series({ settledDays: 20 }), budgets).budget?.budgetId).toBe(
      "tenant-month",
    );
  });

  it("does not fall back to the tenant budget for a scope that has none", () => {
    // A feature with no budget of its own is a normal state, and it is emphatically not covered by
    // the tenant-wide budget: that one is compared against tenant-wide spend.
    const scoped = burndownSection(series({ settledDays: 20 }), [budget(1_000)], feature);
    expect(scoped.budget).toBeNull();
    expect(scoped.varianceMicroUsd).toBeNull();
    expect(scoped.forecast.breach.outcome).toBe("no_budget");
    expect(scoped.noBudgetReason).toContain("feature: research-agent");
    expect(scoped.noBudgetReason).toContain("not measured against the tenant-wide budget");
  });

  it("suppresses layer budgets off tenant-wide scope, with a reason", () => {
    // A layer budget covers that layer across the WHOLE tenant. Comparing one feature's compute
    // projection against it would report a feature as over a budget it does not own.
    const budgets = [
      budget(1_000),
      budget(200, { budget_id: "compute", scope_kind: "layer", scope_value: "compute" }),
    ];
    const tenant = burndownSection(series({ settledDays: 20 }), budgets);
    expect(tenant.layerBudgetReason).toBeNull();
    expect(tenant.layers.find((l) => l.layer === "compute")?.budgetMicroUsd).toBe(200 * USD);

    const scoped = burndownSection(series({ settledDays: 20 }), budgets, feature);
    expect(scoped.layerBudgetReason).toContain("whole tenant");
    expect(scoped.layers.every((l) => l.budgetMicroUsd === null)).toBe(true);
    expect(scoped.layers.every((l) => l.varianceMicroUsd === null)).toBe(true);
  });
});
