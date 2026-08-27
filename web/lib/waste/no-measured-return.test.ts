// SPDX-License-Identifier: Apache-2.0
// Tests for the pure "spend with no measured return" detector (CTO-232, W5; epic CTO-227). Each case
// pins the honesty guarantee that makes this the detector least likely to cry wolf: it flags a
// costly-but-unattributed feature ONLY on a tenant that attributes value somewhere, emits NOTHING
// when the whole tenant has no revenue wired, caps confidence when the tenant's attribution is
// sparse, and never flags a feature that has real measured return. The live SQL in
// `collectNoMeasuredReturn` is exercised against ClickHouse (numbers in the PR body); these cover the
// judgement that shapes its output.

import { describe, expect, it } from "vitest";

import { detectNoMeasuredReturn, type NoReturnEconRow } from "./no-measured-return";

const USD = 1_000_000;

/** Build an econ row with sensible defaults; override only what a case cares about. */
function row(over: Partial<NoReturnEconRow> = {}): NoReturnEconRow {
  return {
    scopeKind: "feature",
    scopeValue: "chatbot",
    windowSpendMicroUsd: 100 * USD,
    attributionRate: null,
    // Default is genuinely-zero attribution (flaggable). Cases that model a converting feature set
    // conversions > 0 explicitly.
    conversions: 0,
    ...over,
  };
}

describe("detectNoMeasuredReturn", () => {
  it("flags a costly, unattributed feature on an otherwise-attributed tenant", () => {
    const findings = detectNoMeasuredReturn(
      [row({ scopeValue: "summarizer", windowSpendMicroUsd: 250 * USD, attributionRate: null })],
      // Tenant attributes value elsewhere and does so healthily.
      0.8,
    );

    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("no_measured_return");
    expect(f.scopeKind).toBe("feature");
    expect(f.scopeValue).toBe("summarizer");
    // Recoverable = the at-risk (windowed) spend, not null and not zero.
    expect(f.recoverableMicroUsd).toBe(250 * USD);
    expect(f.windowSpendMicroUsd).toBe(250 * USD);
    expect(f.drillHref).toBe("/attribution");
    // The reason MUST name the category phrase and carry the investigate-not-verdict caveat.
    expect(f.reason.toLowerCase()).toContain("spend with no measured return");
    expect(f.reason.toLowerCase()).toContain("investigate");
    expect(f.evidence).toMatchObject({
      windowSpend: 250 * USD,
      attributedValue: 0,
      tenantAttributionRate: 0.8,
    });
  });

  it("returns [] when the whole tenant has no revenue wired (rate 0 or null)", () => {
    const rows = [
      row({ scopeValue: "a", windowSpendMicroUsd: 500 * USD, attributionRate: null }),
      row({ scopeValue: "b", windowSpendMicroUsd: 300 * USD, attributionRate: null }),
    ];
    // No business events at all -> null: an instrumentation gap, never a wall of false positives.
    expect(detectNoMeasuredReturn(rows, null)).toEqual([]);
    // Events exist but none attributed -> 0: same honesty gate.
    expect(detectNoMeasuredReturn(rows, 0)).toEqual([]);
  });

  it("caps confidence at low when the tenant's attribution is sparse", () => {
    const sparse = detectNoMeasuredReturn(
      [row({ attributionRate: null })],
      0.1, // some attribution, but thin
    );
    expect(sparse[0].confidence).toBe("low");

    const healthy = detectNoMeasuredReturn(
      [row({ attributionRate: null })],
      0.7, // otherwise well-attributed tenant
    );
    expect(healthy[0].confidence).toBe("medium");
  });

  it("does not flag a feature with real measured return", () => {
    const findings = detectNoMeasuredReturn(
      [
        row({ scopeValue: "paid-search", windowSpendMicroUsd: 400 * USD, attributionRate: 1 }),
        row({ scopeValue: "orphan", windowSpendMicroUsd: 120 * USD, attributionRate: null }),
      ],
      0.9,
    );
    // Only the unattributed one is flagged; the attributed feature is left alone.
    expect(findings.map((f) => f.scopeValue)).toEqual(["orphan"]);
  });

  // CTO-227 review finding (Bug 3): a sparse-but-converting feature has conversions > 0 while its
  // attributionRate is null (below the MIN_CONVERSIONS_FOR_ECONOMICS trust floor). It must NOT be
  // flagged: it DID convert, just sparsely. Only a genuinely-zero-conversion feature is waste.
  it("does not flag a sparse-but-converting feature (conversions > 0, rate null)", () => {
    const findings = detectNoMeasuredReturn(
      [
        // Genuinely unattributed: zero conversions -> flagged.
        row({ scopeValue: "orphan", windowSpendMicroUsd: 300 * USD, attributionRate: null, conversions: 0 }),
        // Converted, but too few to trust the rate (null). Real return -> NOT flagged.
        row({ scopeValue: "sparse", windowSpendMicroUsd: 400 * USD, attributionRate: null, conversions: 3 }),
      ],
      0.8,
    );
    expect(findings.map((f) => f.scopeValue)).toEqual(["orphan"]);
  });

  it("flags a genuinely-zero-conversion feature even when its rate is null", () => {
    const findings = detectNoMeasuredReturn(
      [row({ scopeValue: "dead-weight", windowSpendMicroUsd: 500 * USD, attributionRate: null, conversions: 0 })],
      0.6,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeValue).toBe("dead-weight");
    expect(findings[0].recoverableMicroUsd).toBe(500 * USD);
  });

  it("does not flag a scope with no spend at risk", () => {
    const findings = detectNoMeasuredReturn(
      [row({ scopeValue: "free", windowSpendMicroUsd: 0, attributionRate: null })],
      0.8,
    );
    expect(findings).toEqual([]);
  });
});
