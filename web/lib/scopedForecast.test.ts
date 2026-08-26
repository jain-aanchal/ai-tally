// SPDX-License-Identifier: Apache-2.0
// Scoped budgets and per-scope forecasts (CTO-211). Every test here is one of the ticket's
// correctness or honesty requirements, and all of them fail SILENTLY if they regress: a scope
// projected from tenant-wide history looks exactly like one projected from its own, and a roster
// that double counts adds up to a number that looks perfectly plausible.

import { describe, expect, it } from "vitest";

import type { SettledSeriesLike, TenantBudget } from "./budgetVsActual";
import { burndownSection } from "./burndown";
import { LAYERS } from "./cost";
import { rosterScopes, scopedForecast, type ScopeSection } from "./scopedForecast";
import { TENANT_SCOPE, type ForecastScope } from "./spendScopes";
import type { SpendByLayer } from "./types";

const USD = 1_000_000;
const TODAY = "2026-08-20";
const PERIOD_START = "2026-08-01";

function layers(over: Partial<SpendByLayer> = {}): SpendByLayer {
  return { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0, ...over };
}

function addDays(iso: string, n: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

type DayRow = {
  date: string;
  byLayer: SpendByLayer;
  totalMicroUsd: number;
  inPeriod: boolean;
  settled: boolean;
  inProgress: boolean;
  awaitingLayers: string[];
};

/**
 * A settled series the way `querySettledCostSeries(scope)` returns one: a 30-day window ending
 * today, every day before today settled, and a day with no rows carrying a genuine zero.
 *
 * `firstSpendDay` is the fixture that matters. Days before it are zero-but-settled, which is
 * exactly the shape a scope introduced mid-window arrives in, and exactly what CTO-210's
 * leading-zero trim exists to refuse to count as history.
 */
function series(opts: { perDayUsd: number; firstSpendDay?: string }): SettledSeriesLike {
  const windowStart = addDays(TODAY, -29);
  const first = opts.firstSpendDay ?? windowStart;
  const perDay = opts.perDayUsd * USD;
  const days: DayRow[] = [];
  for (let i = 29; i >= 0; i -= 1) {
    const date = addDays(TODAY, -i);
    const inProgress = date === TODAY;
    const spending = date >= first;
    const amount = spending ? (inProgress ? Math.round(perDay * 0.5) : perDay) : 0;
    days.push({
      date,
      byLayer: layers({ llm: Math.round(amount * 0.7), compute: amount - Math.round(amount * 0.7) }),
      totalMicroUsd: amount,
      inPeriod: date >= PERIOD_START,
      settled: !inProgress,
      inProgress,
      awaitingLayers: [],
    });
  }

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

  return {
    periodStart: PERIOD_START,
    windowEnd: TODAY,
    settledThrough: addDays(TODAY, -1),
    rule: "day-complete",
    connectorLayers: [],
    days,
    periodSettled: totals((d) => d.inPeriod && d.settled),
    periodObserved: totals((d) => d.inPeriod),
  };
}

function budget(over: Partial<TenantBudget> = {}): TenantBudget {
  return {
    budget_id: "b",
    period: "month",
    amount_micro: 10_000 * USD,
    scope_kind: "tenant",
    scope_value: "",
    starts_on: PERIOD_START,
    ends_on: null,
    ...over,
  };
}

function section(
  scope: ForecastScope,
  budgets: readonly TenantBudget[],
  opts: { perDayUsd: number; firstSpendDay?: string },
): ScopeSection {
  return { scope, section: burndownSection(series(opts), budgets, scope) };
}

const FEATURE_A: ForecastScope = { kind: "feature", value: "research-agent" };
const FEATURE_B: ForecastScope = { kind: "feature", value: "support-bot" };

describe("rosterScopes", () => {
  it("always offers tenant-wide first, even with no budget at all", () => {
    expect(rosterScopes([], TODAY)).toEqual([TENANT_SCOPE]);
  });

  it("offers every scope with a monthly budget covering today, ordered by kind then value", () => {
    const roster = rosterScopes(
      [
        budget({ budget_id: "m", scope_kind: "model", scope_value: "gpt-4o" }),
        budget({ budget_id: "b", scope_kind: "feature", scope_value: "support-bot" }),
        budget({ budget_id: "a", scope_kind: "feature", scope_value: "research-agent" }),
      ],
      TODAY,
    );
    expect(roster.map((s) => `${s.kind}:${s.value}`)).toEqual([
      "tenant:",
      "feature:research-agent",
      "feature:support-bot",
      "model:gpt-4o",
    ]);
  });

  it("skips budgets that do not govern this month", () => {
    const roster = rosterScopes(
      [
        // A quarter's dollars against a month of spend reports everyone as comfortably under.
        budget({ budget_id: "q", period: "quarter", scope_kind: "feature", scope_value: "q" }),
        // Ended before today, and starting after it.
        budget({ budget_id: "old", scope_kind: "feature", scope_value: "old", ends_on: "2026-07-31" }),
        budget({ budget_id: "new", scope_kind: "feature", scope_value: "new", starts_on: "2026-09-01" }),
      ],
      TODAY,
    );
    expect(roster).toEqual([TENANT_SCOPE]);
  });
});

describe("scopedForecast: the per-scope history guard", () => {
  // The whole reason this ticket reads a series per scope instead of filtering a tenant one.
  it("refuses to project a feature introduced last week on a mature tenant", () => {
    const budgets = [
      budget({ budget_id: "t" }),
      budget({ budget_id: "f", scope_kind: "feature", scope_value: FEATURE_A.value, amount_micro: 500 * USD }),
    ];
    const result = scopedForecast({
      sections: [
        section(TENANT_SCOPE, budgets, { perDayUsd: 100 }),
        // Five days old: seen spending from the 15th, and today is the 20th.
        section(FEATURE_A, budgets, { perDayUsd: 20, firstSpendDay: "2026-08-15" }),
      ],
      requested: null,
      budgets,
    });

    const tenant = result.lines.find((l) => l.scope.kind === "tenant");
    const feature = result.lines.find((l) => l.scope.kind === "feature");
    // The tenant is mature and projects fine, which is exactly the trap: a tenant-wide day count
    // would have waved the feature through the floor.
    expect(tenant?.status).toBe("ok");
    expect(tenant?.projectedMicroUsd).not.toBeNull();

    expect(feature?.status).toBe("insufficient_history");
    expect(feature?.projectedMicroUsd).toBeNull();
    expect(feature?.historyDays).toBe(5);
    expect(feature?.trimmedLeadingDays).toBe(24);
    expect(feature?.firstObservedDay).toBe("2026-08-15");
    // "We cannot say" is not "on track", and the enum is what stops those collapsing.
    expect(feature?.standing).toBe("unknown");
    expect(feature?.standingReason).toContain("not a statement that it is on track");
    // A budget exists, but no projection does, so there is no projected variance to report.
    expect(feature?.budgetMicroUsd).toBe(500 * USD);
    expect(feature?.varianceMicroUsd).toBeNull();
    expect(feature?.breachDate).toBeNull();
  });

  it("gives a scope with no budget a forecast and no variance, never a variance against zero", () => {
    const budgets = [budget({ budget_id: "t" })];
    const result = scopedForecast({
      sections: [
        section(TENANT_SCOPE, budgets, { perDayUsd: 100 }),
        section(FEATURE_A, budgets, { perDayUsd: 30 }),
      ],
      requested: FEATURE_A,
      budgets,
    });
    const feature = result.lines.find((l) => l.scope.kind === "feature");
    expect(feature?.projectedMicroUsd).toBeGreaterThan(0);
    expect(feature?.budgetMicroUsd).toBeNull();
    expect(feature?.varianceMicroUsd).toBeNull();
    expect(feature?.variancePct).toBeNull();
    expect(feature?.standing).toBe("no_budget");
    // And explicitly not measured against the tenant-wide budget, which covers other dollars too.
    expect(feature?.noBudgetReason).toContain("not measured against the tenant-wide budget");
  });
});

describe("scopedForecast: standings", () => {
  const budgets = [
    budget({ budget_id: "t", amount_micro: 5_000 * USD }),
    // 30/day for 31 days lands near 930; a 2000 budget is comfortably clear.
    budget({ budget_id: "a", scope_kind: "feature", scope_value: FEATURE_A.value, amount_micro: 2_000 * USD }),
    // 30/day against 100 is over before the month is out, and already over month to date.
    budget({ budget_id: "b", scope_kind: "feature", scope_value: FEATURE_B.value, amount_micro: 100 * USD }),
  ];

  const result = scopedForecast({
    sections: [
      section(TENANT_SCOPE, budgets, { perDayUsd: 100 }),
      section(FEATURE_A, budgets, { perDayUsd: 30 }),
      section(FEATURE_B, budgets, { perDayUsd: 30 }),
    ],
    requested: null,
    budgets,
  });

  it("separates on track from projected breach from already over", () => {
    const byKey = new Map(result.lines.map((l) => [l.key, l]));
    expect(byKey.get("feature:research-agent")?.standing).toBe("on_track");
    // Settled month to date already exceeds a 100 budget, which is measured rather than projected
    // and outranks any forecast.
    expect(byKey.get("feature:support-bot")?.standing).toBe("already_over");
    expect(byKey.get("feature:support-bot")?.standingReason).toContain("measured, not projected");
  });

  it("puts tenant-wide first and then orders by settled spend so an owner finds their own line", () => {
    expect(result.lines[0].scope.kind).toBe("tenant");
    const rest = result.lines.slice(1);
    expect(rest.map((l) => l.settledMicroUsd)).toEqual(
      [...rest.map((l) => l.settledMicroUsd)].sort((a, b) => b - a),
    );
  });

  it("reports a projected breach with its date", () => {
    const tight = [
      budget({ budget_id: "t", amount_micro: 5_000 * USD }),
      budget({
        budget_id: "a",
        scope_kind: "feature",
        scope_value: FEATURE_A.value,
        // Above month to date (about 570 by the 19th) but below the month-end projection.
        amount_micro: 700 * USD,
      }),
    ];
    const tightResult = scopedForecast({
      sections: [
        section(TENANT_SCOPE, tight, { perDayUsd: 100 }),
        section(FEATURE_A, tight, { perDayUsd: 30 }),
      ],
      requested: null,
      budgets: tight,
    });
    const line = tightResult.lines.find((l) => l.scope.kind === "feature");
    expect(line?.standing).toBe("projected_breach");
    expect(line?.breachDate).toMatch(/^2026-08-\d\d$/);
    expect(line?.varianceMicroUsd).toBeGreaterThan(0);
  });
});

describe("scopedForecast: the spend must reconcile, the budgets need not", () => {
  const budgets = [
    budget({ budget_id: "t", amount_micro: 1_000 * USD }),
    budget({ budget_id: "a", scope_kind: "feature", scope_value: FEATURE_A.value, amount_micro: 900 * USD }),
    budget({ budget_id: "b", scope_kind: "feature", scope_value: FEATURE_B.value, amount_micro: 900 * USD }),
  ];
  const result = scopedForecast({
    sections: [
      section(TENANT_SCOPE, budgets, { perDayUsd: 100 }),
      section(FEATURE_A, budgets, { perDayUsd: 30 }),
      section(FEATURE_B, budgets, { perDayUsd: 20 }),
    ],
    requested: null,
    budgets,
  });

  it("sums spend only within a kind, and states the residual rather than implying the rows are the bill", () => {
    const { reconciliation } = result;
    expect(reconciliation.groups).toHaveLength(1);
    const group = reconciliation.groups[0];
    expect(group.kind).toBe("feature");
    expect(group.settledMicroUsd + group.residualMicroUsd).toBe(
      reconciliation.tenantSettledMicroUsd,
    );
    expect(group.residualMicroUsd).toBeGreaterThan(0);
    expect(reconciliation.spendReconciles).toBe(true);
    expect(reconciliation.spendWarnings).toEqual([]);
  });

  it("treats feature budgets summing above the tenant budget as normal, not as a warning", () => {
    // 900 + 900 against a 1000 tenant budget. Deliberate over-allocation is a management posture.
    const alloc = result.reconciliation.budgetAllocation;
    expect(alloc.tenantBudgetMicroUsd).toBe(1_000 * USD);
    expect(alloc.perKind).toEqual([{ kind: "feature", budgetMicroUsd: 1_800 * USD, count: 2 }]);
    expect(alloc.overAllocated).toBe(true);
    expect(alloc.note).toContain("allowed and often deliberate");
    // The load-bearing assertion: over-allocated budgets do NOT make the page report a problem.
    expect(result.reconciliation.spendReconciles).toBe(true);
    expect(result.reconciliation.spendWarnings).toEqual([]);
  });

  it("calls scoped spend above the tenant total a bug on the page", () => {
    // Only reachable through a bug (a mis-scoped query, a stale series), which is exactly why it is
    // checked: it would otherwise render as a perfectly plausible number.
    const impossible = scopedForecast({
      sections: [
        section(TENANT_SCOPE, budgets, { perDayUsd: 10 }),
        section(FEATURE_A, budgets, { perDayUsd: 30 }),
      ],
      requested: null,
      budgets,
    });
    expect(impossible.reconciliation.spendReconciles).toBe(false);
    expect(impossible.reconciliation.spendWarnings.join(" ")).toContain("bug on this page");
    expect(impossible.lines.find((l) => l.scope.kind === "feature")?.exceedsTenantSettled).toBe(
      true,
    );
  });

  it("never adds spend across kinds, and says why", () => {
    const mixed = [
      budget({ budget_id: "t", amount_micro: 5_000 * USD }),
      budget({ budget_id: "a", scope_kind: "feature", scope_value: FEATURE_A.value }),
      budget({ budget_id: "m", scope_kind: "model", scope_value: "gpt-4o" }),
    ];
    const mixedResult = scopedForecast({
      sections: [
        section(TENANT_SCOPE, mixed, { perDayUsd: 100 }),
        section(FEATURE_A, mixed, { perDayUsd: 30 }),
        section({ kind: "model", value: "gpt-4o" }, mixed, { perDayUsd: 80 }),
      ],
      requested: null,
      budgets: mixed,
    });
    // 30 + 80 exceeds neither the tenant total nor anything else, because the two are never added:
    // the same dollar is both this feature's and this model's.
    expect(mixedResult.reconciliation.mixesKinds).toBe(true);
    expect(mixedResult.reconciliation.groups.map((g) => g.kind)).toEqual(["feature", "model"]);
    expect(mixedResult.reconciliation.spendReconciles).toBe(true);
  });
});

describe("scopedForecast: selection", () => {
  const budgets = [
    budget({ budget_id: "t" }),
    budget({ budget_id: "a", scope_kind: "feature", scope_value: FEATURE_A.value }),
  ];
  const sections = [
    section(TENANT_SCOPE, budgets, { perDayUsd: 100 }),
    section(FEATURE_A, budgets, { perDayUsd: 30 }),
  ];

  it("defaults to tenant-wide with no fallback reason", () => {
    const result = scopedForecast({ sections, requested: null, budgets });
    expect(result.selected).toEqual(TENANT_SCOPE);
    expect(result.selectionFallbackReason).toBeNull();
    expect(result.section.scope.kind).toBe("tenant");
  });

  it("shows the requested scope's own section", () => {
    const result = scopedForecast({ sections, requested: FEATURE_A, budgets });
    expect(result.section.scope).toEqual(FEATURE_A);
    expect(result.lines.find((l) => l.selected)?.key).toBe("feature:research-agent");
  });

  it("falls back to tenant-wide out loud when the requested scope is not on the roster", () => {
    const result = scopedForecast({ sections, requested: FEATURE_B, budgets });
    expect(result.selected).toEqual(TENANT_SCOPE);
    expect(result.selectionFallbackReason).toContain("support-bot");
    expect(result.selectionFallbackReason).toContain("tenant-wide forecast is shown instead");
  });

  it("refuses to assemble anything without the tenant-wide section", () => {
    // It is the denominator of every share and the total every group is checked against.
    expect(() =>
      scopedForecast({ sections: [sections[1]], requested: null, budgets }),
    ).toThrow(/tenant-wide section is required/);
  });
});
