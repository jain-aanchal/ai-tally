// SPDX-License-Identifier: Apache-2.0
// The three states that have to be right on this card (CTO-209): no budget configured, a budget
// with an over-variance, and the comparison being unreadable. Each is an honesty requirement of the
// ticket, and each is a state that fails silently rather than loudly if it regresses.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BLANK } from "@/components/HonestValue";
import type { BudgetVsActual } from "@/lib/budgetVsActual";
import { budgetVsActual, type SettledSeriesLike, type TenantBudget } from "@/lib/budgetVsActual";
import { LAYERS } from "@/lib/cost";
import type { SpendByLayer } from "@/lib/types";

import { BudgetVsActualCard } from "./BudgetVsActualCard";

const USD = 1_000_000;

function layers(over: Partial<SpendByLayer> = {}): SpendByLayer {
  return { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0, ...over };
}

/** Two settled days (600 total) plus one unsettled day (100) inside the month. */
function testSeries(): SettledSeriesLike {
  const days = [
    { date: "2026-08-01", byLayer: layers({ llm: 200 * USD, compute: 100 * USD }), settled: true },
    { date: "2026-08-02", byLayer: layers({ llm: 200 * USD, compute: 100 * USD }), settled: true },
    { date: "2026-08-03", byLayer: layers({ llm: 100 * USD }), settled: false },
  ].map((d) => ({
    ...d,
    totalMicroUsd: LAYERS.reduce((s, l) => s + d.byLayer[l], 0),
    inPeriod: true,
    inProgress: d.date === "2026-08-03",
    awaitingLayers: [] as string[],
  }));
  const totals = (keep: (d: (typeof days)[number]) => boolean) => {
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
    periodStart: "2026-08-01",
    windowEnd: "2026-08-03",
    settledThrough: "2026-08-02",
    rule: "connector-landing",
    connectorLayers: ["compute", "egress"],
    days,
    periodSettled: totals((d) => d.settled),
    periodObserved: totals(() => true),
  };
}

const BUDGET: TenantBudget = {
  budget_id: "tenant-month",
  period: "month",
  amount_micro: 500 * USD,
  scope_kind: "tenant",
  scope_value: "",
  starts_on: "2026-01-01",
  ends_on: null,
};

function comparison(budgets: TenantBudget[]): BudgetVsActual {
  return budgetVsActual(testSeries(), budgets);
}

describe("BudgetVsActualCard", () => {
  it("shows the actual and a blank variance, not a variance against zero, with no budget set", () => {
    render(<BudgetVsActualCard payload={{ comparison: comparison([]), unavailable: null }} />);

    expect(screen.getAllByText("$600.00").length).toBeGreaterThan(0);
    // The reason is on the blank itself, so a reader can find out why the cell is empty.
    const blanks = screen.getAllByText(BLANK);
    expect(blanks.length).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/No value: no monthly tenant-wide budget is set/).length,
    ).toBeGreaterThan(0);
    // No fabricated over/under language: the headline is the blank itself.
    expect(screen.getByTestId("budget-variance-headline").textContent).toContain(BLANK);
    // And a way to fix it.
    const link = screen.getByRole("link", { name: /set a monthly budget/i });
    expect(link.getAttribute("href")).toBe("/settings/budgets");
  });

  it("leads with the variance sign, in words as well as colour", () => {
    render(<BudgetVsActualCard payload={{ comparison: comparison([BUDGET]), unavailable: null }} />);

    // 600 settled against a 500 budget: 100 over, 20 percent.
    const headline = screen.getByTestId("budget-variance-headline");
    expect(headline.textContent).toBe("+$100.00 over");
    expect(headline.className).toContain("text-bad");
    expect(screen.getAllByText(/20.0/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("$500.00").length).toBeGreaterThan(0);
  });

  it("states the days it covers and the days it excludes", () => {
    render(<BudgetVsActualCard payload={{ comparison: comparison([BUDGET]), unavailable: null }} />);

    const covers = screen.getByText(/Counts 2 settled days/);
    expect(covers.textContent).toContain("2026-08-01");
    expect(covers.textContent).toContain("2026-08-02");
    expect(covers.textContent).toContain("connector-landing");
    // The exclusion has to be quantified: without it this $600 contradicts the 30-day headline
    // directly above it on the same page.
    const excludes = screen.getByText(/Excludes 1 day/);
    expect(excludes.textContent).toContain("2026-08-03");
    expect(excludes.textContent).toContain("$100.00");
  });

  it("says it could not read the comparison rather than showing a zero", () => {
    render(
      <BudgetVsActualCard
        payload={{ comparison: null, unavailable: "ClickHouse is unreachable" }}
      />,
    );
    expect(screen.getAllByText(/ClickHouse is unreachable/).length).toBeGreaterThan(0);
    expect(screen.queryByText("$0.00")).toBeNull();
  });
});
