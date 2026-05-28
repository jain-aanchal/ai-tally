import { describe, expect, it } from "vitest";
import { comparison, deltaPct } from "./compare";

describe("compare", () => {
  it("deltaPct: negative when candidate cheaper than current", () => {
    expect(deltaPct(100, 28)).toBeCloseTo(-0.72, 2);
  });

  it("deltaPct: positive when candidate is worse (latency)", () => {
    expect(deltaPct(1000, 1500)).toBeCloseTo(0.5, 2);
  });

  it("deltaPct: zero baseline returns 0 (no divide-by-zero)", () => {
    expect(deltaPct(0, 100)).toBe(0);
  });

  it("mock comparison has at least 2 candidates and a recommendation", () => {
    expect(comparison.candidates.length).toBeGreaterThanOrEqual(2);
    expect(comparison.recommendation.verdict).toBeDefined();
    expect(comparison.recommendation.projectedSavingsMicroUsd).toBeGreaterThan(0);
  });

  it("diagnostics excludes throttled samples from headline metrics (modeled)", () => {
    expect(comparison.diagnostics.excludedRateLimited).toBeGreaterThan(0);
    expect(comparison.diagnostics.contextFidelity).toMatch(/resolved-context/);
  });
});
