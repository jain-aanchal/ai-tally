// SPDX-License-Identifier: Apache-2.0
// The four states that have to be right on this section (CTO-210): a projected breach with its
// date, a projection that stays under, no budget configured, and not enough history to project at
// all. Each is an honesty requirement of the ticket, and the last two are the ones that regress
// quietly: rendering a cone below the history floor, or letting "we cannot say" read like "you are
// fine", both look completely normal on screen.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SettledSeriesLike, TenantBudget } from "@/lib/budgetVsActual";
import { burndownSection } from "@/lib/burndown";
import { LAYERS } from "@/lib/cost";
import { scopedForecast, type ScopeSection } from "@/lib/scopedForecast";
import { TENANT_SCOPE, type ForecastScope } from "@/lib/spendScopes";
import type { SpendByLayer } from "@/lib/types";

import { BurndownCard } from "./BurndownCard";

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

/** `settledDays` complete days ending yesterday, plus today still accruing. */
function series(settledDays: number, today = "2026-08-20", perDayUsd = 100): SettledSeriesLike {
  const perDay = perDayUsd * USD;
  const days: DayRow[] = [];
  for (let i = settledDays; i >= 1; i -= 1) {
    days.push({
      date: addDays(today, -i),
      byLayer: layers({
        llm: Math.round(perDay * 0.7),
        compute: perDay - Math.round(perDay * 0.7),
      }),
      totalMicroUsd: perDay,
      inPeriod: addDays(today, -i) >= "2026-08-01",
      settled: true,
      inProgress: false,
      awaitingLayers: [],
    });
  }
  days.push({
    date: today,
    byLayer: layers({
      llm: Math.round(perDay * 0.35),
      compute: Math.round(perDay * 0.5) - Math.round(perDay * 0.35),
    }),
    totalMicroUsd: Math.round(perDay * 0.5),
    inPeriod: true,
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
    periodStart: "2026-08-01",
    windowEnd: today,
    settledThrough: settledDates[settledDates.length - 1] ?? null,
    rule: "connector-landing",
    connectorLayers: ["compute", "egress"],
    days,
    periodSettled: totals((d) => d.inPeriod && d.settled),
    periodObserved: totals((d) => d.inPeriod),
  };
}

function budget(amountUsd: number): TenantBudget {
  return {
    budget_id: "tenant-month",
    period: "month",
    amount_micro: amountUsd * USD,
    scope_kind: "tenant",
    scope_value: "",
    starts_on: "2026-01-01",
    ends_on: null,
  };
}

function renderSection(settledDays: number, budgets: TenantBudget[]) {
  return render(
    <BurndownCard
      payload={{ section: burndownSection(series(settledDays), budgets), unavailable: null }}
    />,
  );
}

describe("BurndownCard", () => {
  it("labels the breach date on the chart and in the headline", () => {
    const { container } = renderSection(20, [budget(2_400)]);
    // In the headline, in words: the single most useful output of the feature.
    expect(screen.getByTestId("burndown-headline").textContent).toContain(
      "Crosses budget 2026-08-24",
    );
    // And on the chart itself, so it survives being screenshotted out of context.
    expect(container.querySelector("svg")?.textContent).toContain("crosses budget 2026-08-24");
    // The chart's own sentence names the outcome rather than describing shapes.
    expect(container.querySelector("svg")?.getAttribute("aria-label")).toContain(
      "crossing the",
    );
  });

  it("says a projection that stays under budget is a projection, not a guarantee", () => {
    const { container } = renderSection(20, [budget(100_000)]);
    const headline = screen.getByTestId("burndown-headline").textContent ?? "";
    expect(headline).toContain("None this period");
    expect(headline).toContain("forecast, not a commitment");
    // No breach marker anywhere on the chart.
    expect(container.querySelector("svg")?.textContent).not.toContain("crosses budget");
  });

  it("renders no budget as its own state, with a way to set one and no invented variance", () => {
    renderSection(20, []);
    const headline = screen.getByTestId("burndown-headline").textContent ?? "";
    expect(headline).toContain("No budget is set");
    expect(headline).toContain("rather than compared against a budget of zero");
    expect(screen.getByRole("link", { name: /set a monthly budget/i })).toHaveProperty("href");
    // A projection is still shown: no budget is not a reason to withhold the forecast.
    expect(headline).toMatch(/\$/);
  });

  it("draws NO cone below the minimum history and says it is not a claim of safety", () => {
    const { container } = renderSection(5, [budget(2_400)]);
    const panel = screen.getByTestId("burndown-insufficient-history").textContent ?? "";
    expect(panel).toContain("Not enough history to project");
    expect(panel).toContain("5 settled days");
    expect(panel).toContain("14");
    // The load-bearing sentence: `cannot_project` must not read like `never`.
    expect(panel).toContain("This is not a statement that spend is under control");
    // No chart at all. Not a faint one, not an empty frame: none.
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.queryByTestId("burndown-headline")).toBeNull();
  });

  it("states the input window in every state, including the refusal", () => {
    const { container: projected } = renderSection(20, [budget(2_400)]);
    expect(projected.textContent).toContain("Projected from 20 settled days");
    expect(projected.textContent).toContain("2026-08-19");
    expect(projected.textContent).toContain("rule: connector-landing");
    // The excluded in-progress day, quantified in dollars rather than merely mentioned.
    expect(projected.textContent).toContain("2026-08-20");
    expect(projected.textContent).toMatch(/Excludes 1 day/);

    const { container: refused } = renderSection(5, [budget(2_400)]);
    expect(refused.textContent).toContain("Projected from 5 settled days");
    expect(refused.textContent).toContain("rule: connector-landing");
  });

  it("names the layer behind the projection rather than only the total", () => {
    const { container } = renderSection(20, [budget(2_400)]);
    expect(container.textContent).toContain("By layer: what the projection is made of");
    // 70/30 llm/compute in the fixture, so LLM is the reason.
    expect(container.textContent).toMatch(/LLM is the largest part of it/);
    expect(container.textContent).toContain("Share of projection");
  });

  it("blanks the whole section with a reason when the data could not be read", () => {
    render(
      <BurndownCard
        payload={{ section: null, unavailable: "ClickHouse is unreachable from the dashboard" }}
      />,
    );
    expect(screen.getByText(/No forecast:/).textContent).toContain(
      "ClickHouse is unreachable from the dashboard",
    );
  });
});

// CTO-211 (F7). The states that have to be distinguishable on screen when several budgets exist:
// one on track, one projected to breach, one with too little history of its own, and the two
// reconciliation registers (spend is a bug when it does not add up, budgets are not).
describe("BurndownCard, scoped (CTO-211)", () => {
  function scopedBudget(
    budgetId: string,
    amountUsd: number,
    scopeKind: string,
    scopeValue: string,
  ): TenantBudget {
    return {
      budget_id: budgetId,
      period: "month",
      amount_micro: amountUsd * USD,
      scope_kind: scopeKind,
      scope_value: scopeValue,
      starts_on: "2026-01-01",
      ends_on: null,
    };
  }

  // The tenant spends 100/day; the three features spend 30, 25 and 10, so the scoped spend adds up
  // to less than the tenant's, which is the only way it is allowed to add up.
  const budgets = [
    budget(4_000),
    scopedBudget("healthy", 2_000, "feature", "research-agent"),
    // 25/day lands near 775 by month end, over a 700 budget but not over it yet.
    scopedBudget("tight", 700, "feature", "support-bot"),
    scopedBudget("new", 900, "feature", "brand-new"),
  ];

  function scopedPayload(requested: ForecastScope | null) {
    const sections: ScopeSection[] = [
      { scope: TENANT_SCOPE, section: burndownSection(series(20), budgets) },
      {
        scope: { kind: "feature", value: "research-agent" },
        section: burndownSection(series(20, "2026-08-20", 30), budgets, {
          kind: "feature",
          value: "research-agent",
        }),
      },
      {
        scope: { kind: "feature", value: "support-bot" },
        section: burndownSection(series(20, "2026-08-20", 25), budgets, {
          kind: "feature",
          value: "support-bot",
        }),
      },
      {
        // Five settled days of its own: the feature introduced last week on a mature tenant.
        scope: { kind: "feature", value: "brand-new" },
        section: burndownSection(series(5, "2026-08-20", 10), budgets, {
          kind: "feature",
          value: "brand-new",
        }),
      },
    ];
    return scopedForecast({ sections, requested, budgets });
  }

  function renderScoped(requested: ForecastScope | null) {
    const result = scopedPayload(requested);
    return render(
      <BurndownCard
        payload={{ section: result.section, unavailable: null }}
        scoped={{ scoped: result, unavailable: null }}
      />,
    );
  }

  it("offers every budgeted scope, defaulting to tenant-wide", () => {
    const { container } = renderScoped(null);
    const selector = screen.getByTestId("forecast-scope-selector");
    expect(selector.textContent).toContain("Whole tenant");
    expect(selector.textContent).toContain("feature: research-agent");
    expect(selector.textContent).toContain("feature: brand-new");
    expect(selector.querySelector('[aria-current="page"]')?.textContent).toContain("Whole tenant");
    // Tenant-wide is the default, so the section reads exactly as it did before this ticket.
    expect(container.querySelector('[data-testid="forecast-scope-heading"]')).toBeNull();
  });

  it("shows on track, projected breach and cannot-say as three different standings", () => {
    const { container } = renderScoped(null);
    const table = container.querySelector('[data-testid="forecast-scope-roster"]');
    expect(table?.textContent).toContain("On track");
    expect(table?.textContent).toContain("Projected breach");
    // Not "On track" and not green: refusing to project is not a clean bill of health.
    expect(table?.textContent).toContain("Cannot say");
  });

  it("blanks the variance for a scope with too little history, saying which it is", () => {
    const { container } = renderScoped(null);
    const table = container.querySelector('[data-testid="forecast-scope-roster"]');
    const reasons = [...(table?.querySelectorAll("span[title]") ?? [])].map((n) =>
      n.getAttribute("title"),
    );
    expect(reasons.join(" ")).toContain("settled days of its own history");
    expect(reasons.join(" ")).toContain("no crossing date is claimed either way");
  });

  it("renders the selected scope's own section and says the card above is tenant-wide", () => {
    const { container } = renderScoped({ kind: "feature", value: "support-bot" });
    const heading = container.querySelector('[data-testid="forecast-scope-heading"]');
    expect(heading?.textContent).toContain("feature: support-bot");
    expect(heading?.textContent).toContain("month-to-date card above stays tenant-wide");
  });

  it("renders the per-scope refusal for a feature introduced last week", () => {
    const { container } = renderScoped({ kind: "feature", value: "brand-new" });
    const refusal = container.querySelector('[data-testid="burndown-insufficient-history"]');
    expect(refusal?.textContent).toContain("feature: brand-new has 5 settled days of history");
    expect(refusal?.textContent).toContain("not a statement that spend is under control");
    // No cone, per requirement 1, and per scope now.
    expect(container.querySelector("svg")).toBeNull();
  });

  it("states over-allocated budgets neutrally and never as a warning", () => {
    const { container } = renderScoped(null);
    const alloc = container.querySelector('[data-testid="forecast-budget-allocation"]');
    // 2000 + 700 + 900 against a 4000 tenant budget is under; the wording is still the neutral one.
    expect(alloc?.textContent).toContain("budgets need not add up");
    expect(alloc?.textContent).toContain("The spend below does");
    // The spend does reconcile here, so no warning is rendered at all.
    expect(container.textContent).not.toContain("bug on this page");
  });
});
