import { describe, expect, it } from "vitest";
import { pctDelta, projection } from "./estimate";

describe("estimate", () => {
  it("pctDelta returns 0 for zero baseline (no divide-by-zero)", () => {
    expect(pctDelta(0, 100)).toBe(0);
  });

  it("driver breakdown reasons are non-empty + at least one increase", () => {
    expect(projection.drivers.length).toBeGreaterThan(0);
    expect(projection.drivers.some((d) => d.delta > 0)).toBe(true);
  });

  it("driver breakdown roughly explains the monthly delta (within 20%)", () => {
    const sum = projection.drivers.reduce((s, d) => s + d.delta, 0);
    const actual =
      projection.proposed.monthlyCostMicroUsd - projection.current.monthlyCostMicroUsd;
    const rel = Math.abs(sum - actual) / Math.abs(actual);
    expect(rel).toBeLessThan(0.2);
  });

  it("blow-up risk in [0, 1]", () => {
    expect(projection.blowUpRisk).toBeGreaterThanOrEqual(0);
    expect(projection.blowUpRisk).toBeLessThanOrEqual(1);
  });

  it("sample includes pathological runs by design", () => {
    expect(projection.sample.pathologicalIncluded).toBeGreaterThan(0);
  });
});
