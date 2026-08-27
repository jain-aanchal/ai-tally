// SPDX-License-Identifier: Apache-2.0
// Unit tests for the pure paid-for-nothing detector (CTO-229, epic CTO-227). We test only the pure
// `detectPaidForNothing`; the query and `tryLive` wiring in `collectPaidForNothing` are exercised
// against the live stack (see the PR body), not mocked here.

import { describe, it, expect } from "vitest";
import {
  detectPaidForNothing,
  type PaidForNothingRow,
} from "./paid-for-nothing";
import { aggregateWaste } from "@/lib/waste";

/** A fully-specified fixture row; individual tests override just the fields they care about. */
function row(overrides: Partial<PaidForNothingRow>): PaidForNothingRow {
  return {
    scopeKind: "feature",
    scopeValue: "search",
    wastedCost: "0",
    scopeCost: "0",
    failedRuns: "0",
    abandonedRuns: "0",
    exampleTrace: "",
    ...overrides,
  };
}

describe("detectPaidForNothing", () => {
  it("flags a failed billed run with the wasted amount as the recoverable", () => {
    const findings = detectPaidForNothing([
      row({
        scopeKind: "feature",
        scopeValue: "search",
        wastedCost: "0.25", // $0.25 wasted
        scopeCost: "1.00", // $1.00 total on the scope
        failedRuns: "3",
        exampleTrace: "abc123",
      }),
    ]);

    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("paid_for_nothing");
    expect(f.confidence).toBe("high");
    expect(f.scopeKind).toBe("feature");
    expect(f.scopeValue).toBe("search");
    // Recoverable = wasted spend, integer micro-USD, always bounded (never null).
    expect(f.recoverableMicroUsd).toBe(250_000);
    expect(f.windowSpendMicroUsd).toBe(1_000_000);
    expect(f.drillHref).toBe("/agents");
    expect(f.evidence.failedRuns).toBe(3);
    expect(f.evidence.abandonedRuns).toBe(0);
    expect(f.evidence.exampleTrace).toBe("abc123");
    // 0.25 / 1.00 = 25%.
    expect(f.evidence.shareOfScopeSpend).toBe(25);
  });

  it("does not flag a successful run (no wasted spend)", () => {
    const findings = detectPaidForNothing([
      row({ wastedCost: "0", scopeCost: "2.00", failedRuns: "0" }),
    ]);
    expect(findings).toEqual([]);
  });

  it("does not flag a failed run that cost nothing (zero billed)", () => {
    // A run can fail before consuming billable tokens; there is nothing to recover, so no finding.
    const findings = detectPaidForNothing([
      row({ wastedCost: "0", scopeCost: "0", failedRuns: "1" }),
    ]);
    expect(findings).toEqual([]);
  });

  it("groups by feature and by agent independently", () => {
    const findings = detectPaidForNothing([
      row({
        scopeKind: "feature",
        scopeValue: "search",
        wastedCost: "0.10",
        scopeCost: "0.40",
        failedRuns: "1",
      }),
      row({
        scopeKind: "agent",
        scopeValue: "aider",
        wastedCost: "0.30",
        scopeCost: "0.60",
        failedRuns: "2",
      }),
    ]);

    expect(findings).toHaveLength(2);
    const byScope = Object.fromEntries(findings.map((f) => [f.scopeKind, f]));
    expect(byScope.feature.scopeValue).toBe("search");
    expect(byScope.feature.recoverableMicroUsd).toBe(100_000);
    expect(byScope.agent.scopeValue).toBe("aider");
    expect(byScope.agent.recoverableMicroUsd).toBe(300_000);
    expect(byScope.agent.evidence.shareOfScopeSpend).toBe(50);
  });

  // CTO-227 review finding (Bug 2): a single failed feature-tagged run must produce exactly ONE
  // finding and be counted ONCE in the roll-up total. The old SQL rolled the same runs up BY feature
  // AND BY agent, so the run surfaced as two findings and its recoverable was summed twice, overstating
  // "Recoverable" up to 2x. The query now assigns each run to one scope; this locks the single-count
  // guarantee at the row -> detector -> aggregate boundary the endpoint actually uses.
  it("counts a single feature-tagged wasted run once in the aggregate total", () => {
    // The single-scope query yields ONE row for a feature-tagged run (no overlapping agent row).
    const rows: PaidForNothingRow[] = [
      row({
        scopeKind: "feature",
        scopeValue: "search",
        wastedCost: "0.25",
        scopeCost: "1.00",
        failedRuns: "1",
        exampleTrace: "trace-1",
      }),
    ];
    const findings = detectPaidForNothing(rows);
    expect(findings).toHaveLength(1);

    const report = aggregateWaste(findings, 30);
    // Counted exactly once: total equals the single wasted amount, not double it.
    expect(report.totalRecoverableMicroUsd).toBe(250_000);
    expect(report.byCategory.paid_for_nothing).toBe(250_000);
  });

  // CTO-227 review pass 2 (Fix A): the per-scope DENOMINATOR is the scope's TRUE total spend, not the
  // single-scoped subtotal. For an agent, that total includes the feature-tagged runs on it that the
  // wasted-cost roll-up attributed to a feature scope. The query now supplies that true total as
  // `scopeCost`; here we lock the detector's contract on it: an agent whose $0.20 wasted spend is 20%
  // of its real $1.00 total must report windowSpend = full $1.00 and share = 20%, NOT windowSpend =
  // $0.20 and share = 100% (the pre-fix behaviour when scopeCost excluded feature-tagged runs).
  it("uses the agent's true total spend as the share denominator (not the single-scoped subtotal)", () => {
    const findings = detectPaidForNothing([
      row({
        scopeKind: "agent",
        scopeValue: "aider",
        wastedCost: "0.20", // $0.20 wasted (single-scoped, counted once)
        // True agent total over the window: includes this agent's feature-tagged runs too.
        scopeCost: "1.00", // $1.00 real total on the agent
        failedRuns: "2",
        exampleTrace: "trace-agent",
      }),
    ]);

    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.scopeKind).toBe("agent");
    expect(f.scopeValue).toBe("aider");
    expect(f.recoverableMicroUsd).toBe(200_000);
    // Full agent spend, not the $0.20 single-scoped subtotal.
    expect(f.windowSpendMicroUsd).toBe(1_000_000);
    // 0.20 / 1.00 = 20%, not the 100% the pre-fix (subtotal) denominator produced.
    expect(f.evidence.shareOfScopeSpend).toBe(20);
    expect(f.reason).toContain("20%");
  });

  // CTO-227 review pass 3 (Fix 1+2): the single-grouped-pass query carries each scope's wasted cost as
  // a subset of the SAME aggregate's total cost, so a scope can never surface windowSpend < recoverable
  // (the old scope_totals LEFT JOIN could 0-fill the total on clause drift, yielding windowSpend = 0
  // with recoverable > 0, a fabricated 0). Lock that invariant at the detector boundary: over a mix of
  // feature and agent rows where wasted <= total (as the query guarantees), every emitted finding has
  // windowSpendMicroUsd >= recoverableMicroUsd.
  it("never emits a scope whose windowSpend is below its recoverable", () => {
    const findings = detectPaidForNothing([
      row({ scopeKind: "feature", scopeValue: "search", wastedCost: "0.25", scopeCost: "1.00", failedRuns: "3" }),
      row({ scopeKind: "agent", scopeValue: "aider", wastedCost: "0.30", scopeCost: "0.60", failedRuns: "2" }),
      row({ scopeKind: "agent", scopeValue: "loader", wastedCost: "0.05", scopeCost: "0.05", failedRuns: "1" }),
    ]);
    expect(findings).toHaveLength(3);
    for (const f of findings) {
      // Both are real numbers here (observed spend, observed waste), never the honest-blank null.
      expect(f.windowSpendMicroUsd).not.toBeNull();
      expect(f.recoverableMicroUsd).not.toBeNull();
      const windowSpend = f.windowSpendMicroUsd ?? -1;
      const recoverable = f.recoverableMicroUsd ?? 0;
      expect(windowSpend).toBeGreaterThanOrEqual(recoverable);
    }
  });

  it("counts abandoned runs into the wasted-run tally when present", () => {
    const findings = detectPaidForNothing([
      row({
        wastedCost: "0.05",
        scopeCost: "0.05",
        failedRuns: "1",
        abandonedRuns: "2",
      }),
    ]);
    expect(findings).toHaveLength(1);
    expect(findings[0].evidence.failedRuns).toBe(1);
    expect(findings[0].evidence.abandonedRuns).toBe(2);
    // 100% of this scope's spend was wasted.
    expect(findings[0].evidence.shareOfScopeSpend).toBe(100);
  });
});
