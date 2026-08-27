// SPDX-License-Identifier: Apache-2.0
// Tests for the waste-detection roll-up (CTO-228, epic CTO-227). Each case pins one honesty or
// determinism guarantee that would otherwise regress silently: an unbounded finding must stay OUT of
// the total and sort LAST, an all-unbounded report must read as a blank (null) not a zero total, and
// identical findings from independent detectors must collapse to one.

import { describe, expect, it } from "vitest";

import { aggregateWaste, type WasteCategory, type WasteFinding } from "./waste";

const USD = 1_000_000;

/** Build a finding with sensible defaults; override only what a case cares about. */
function finding(over: Partial<WasteFinding> = {}): WasteFinding {
  return {
    category: "paid_for_nothing",
    scopeKind: "feature",
    scopeValue: "chatbot",
    recoverableMicroUsd: 10 * USD,
    windowSpendMicroUsd: 100 * USD,
    confidence: "high",
    title: "Idle spend",
    reason: "spend with no successful runs",
    evidence: {},
    drillHref: null,
    ...over,
  };
}

describe("aggregateWaste", () => {
  it("rolls up recoverable per category", () => {
    const report = aggregateWaste(
      [
        finding({ category: "paid_for_nothing", scopeValue: "a", recoverableMicroUsd: 10 * USD }),
        finding({ category: "paid_for_nothing", scopeValue: "b", recoverableMicroUsd: 5 * USD }),
        finding({ category: "duplicated_work", scopeValue: "c", recoverableMicroUsd: 7 * USD }),
      ],
      30,
    );

    expect(report.byCategory.paid_for_nothing).toBe(15 * USD);
    expect(report.byCategory.duplicated_work).toBe(7 * USD);
    expect(report.totalRecoverableMicroUsd).toBe(22 * USD);
    expect(report.generatedForWindowDays).toBe(30);
    expect(report.unavailable).toBeNull();
  });

  it("populates every category key, null for categories with no findings", () => {
    const report = aggregateWaste(
      [finding({ category: "paid_for_nothing", recoverableMicroUsd: 3 * USD })],
      7,
    );
    const cats: WasteCategory[] = [
      "paid_for_nothing",
      "duplicated_work",
      "wrong_sized_model",
      "no_measured_return",
      "structural_inefficiency",
    ];
    for (const c of cats) expect(c in report.byCategory).toBe(true);
    expect(report.byCategory.paid_for_nothing).toBe(3 * USD);
    expect(report.byCategory.wrong_sized_model).toBeNull();
  });

  it("keeps a null recoverable out of the total and sorts it last", () => {
    const report = aggregateWaste(
      [
        finding({ scopeValue: "unbounded", recoverableMicroUsd: null }),
        finding({ scopeValue: "small", recoverableMicroUsd: 2 * USD }),
        finding({ scopeValue: "big", recoverableMicroUsd: 9 * USD }),
      ],
      30,
    );

    // Total counts only the two bounded findings.
    expect(report.totalRecoverableMicroUsd).toBe(11 * USD);
    // Sorted: big, small, then the unbounded one last.
    expect(report.findings.map((f) => f.scopeValue)).toEqual(["big", "small", "unbounded"]);
    expect(report.findings[2].recoverableMicroUsd).toBeNull();
  });

  it("reports byCategory null when a category has findings but none are bounded", () => {
    const report = aggregateWaste(
      [
        finding({ category: "no_measured_return", scopeValue: "x", recoverableMicroUsd: null }),
        finding({ category: "no_measured_return", scopeValue: "y", recoverableMicroUsd: null }),
      ],
      30,
    );
    expect(report.byCategory.no_measured_return).toBeNull();
    expect(report.totalRecoverableMicroUsd).toBeNull();
  });

  it("returns null total when every finding is unbounded", () => {
    const report = aggregateWaste(
      [
        finding({ scopeValue: "a", recoverableMicroUsd: null }),
        finding({ scopeValue: "b", recoverableMicroUsd: null }),
      ],
      30,
    );
    expect(report.totalRecoverableMicroUsd).toBeNull();
    expect(report.findings).toHaveLength(2);
  });

  it("de-dupes findings identical on category+scopeKind+scopeValue+title", () => {
    const dupe = () =>
      finding({
        category: "duplicated_work",
        scopeKind: "agent",
        scopeValue: "summarizer",
        title: "Redundant retries",
        recoverableMicroUsd: 4 * USD,
      });
    const report = aggregateWaste([dupe(), dupe(), dupe()], 30);

    expect(report.findings).toHaveLength(1);
    expect(report.byCategory.duplicated_work).toBe(4 * USD);
    expect(report.totalRecoverableMicroUsd).toBe(4 * USD);
  });

  it("does not treat findings differing only in title as duplicates", () => {
    const report = aggregateWaste(
      [
        finding({ title: "One", recoverableMicroUsd: 1 * USD }),
        finding({ title: "Two", recoverableMicroUsd: 1 * USD }),
      ],
      30,
    );
    expect(report.findings).toHaveLength(2);
    expect(report.totalRecoverableMicroUsd).toBe(2 * USD);
  });

  it("empty input yields an all-null report", () => {
    const report = aggregateWaste([], 90);
    expect(report.findings).toHaveLength(0);
    expect(report.totalRecoverableMicroUsd).toBeNull();
    expect(report.byCategory.paid_for_nothing).toBeNull();
    expect(report.generatedForWindowDays).toBe(90);
    expect(report.unavailable).toBeNull();
  });
});
