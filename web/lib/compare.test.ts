// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import {
  comparison,
  deltaPct,
  deriveRecommendation,
  deriveWorkload,
  MIN_SAMPLES_TO_RECOMMEND,
  type RecommendationCandidate,
} from "./compare";

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

// CTO-168: pure derivation helpers that ground the workload label + recommendation on live data.
describe("deriveWorkload", () => {
  it("uses the feature tag + window when a tag is present", () => {
    expect(deriveWorkload("research_agent", 7)).toBe(
      "research_agent / production / last 7 days",
    );
  });

  it("falls back to 'all traffic' when the tag is absent or blank", () => {
    expect(deriveWorkload(undefined, 7)).toBe("all traffic / production / last 7 days");
    expect(deriveWorkload("   ", 30)).toBe("all traffic / production / last 30 days");
  });
});

describe("deriveRecommendation", () => {
  const cand = (over: Partial<RecommendationCandidate>): RecommendationCandidate => ({
    model: "claude-haiku-4-5",
    monthlyCostMicroUsd: 3_000_000,
    qualityScore: null,
    latencyP95Ms: 1500,
    ...over,
  });

  it("verdict 'switch' when meaningfully cheaper and wins the majority of judged pairs", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [cand({ qualityScore: 0.62 })],
      samplesReplayed: 60,
    });
    expect(r.verdict).toBe("switch");
    expect(r.summary).toContain("Switch to claude-haiku-4-5");
    expect(r.projectedSavingsMicroUsd).toBe(7_000_000);
    expect(r.projectedSavingsPct).toBeCloseTo(0.7, 6);
  });

  it("verdict 'mixed' (cost win, quality regression) when the candidate wins < 50% of pairs", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [cand({ qualityScore: 0.41 })],
      samplesReplayed: 60,
    });
    expect(r.verdict).toBe("mixed");
    expect(r.summary).toContain("wins only 41%");
  });

  it("verdict 'mixed' (quality unknown) when no eval has judged the cheapest candidate", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [cand({ qualityScore: null })],
      samplesReplayed: 60,
    });
    expect(r.verdict).toBe("mixed");
    expect(r.summary).toContain("run an eval pass");
  });

  it("verdict 'keep' when the cheapest candidate saves less than 5%", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [cand({ monthlyCostMicroUsd: 9_700_000, qualityScore: 0.7 })],
      samplesReplayed: 60,
    });
    expect(r.verdict).toBe("keep");
    expect(r.summary).toContain("Keep claude-sonnet-4-5");
  });

  it("honest 'insufficient data' below the recommend floor — never fabricated prose", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [cand({ qualityScore: 0.9 })],
      samplesReplayed: MIN_SAMPLES_TO_RECOMMEND - 1,
    });
    expect(r.summary).toMatch(/insufficient replay data/i);
    // Savings still projected for display even when we won't issue a confident verdict.
    expect(r.projectedSavingsMicroUsd).toBe(7_000_000);
  });

  it("no candidates → keep current, savings zero", () => {
    const r = deriveRecommendation({
      currentModel: "claude-sonnet-4-5",
      currentCostMicroUsd: 10_000_000,
      candidates: [],
      samplesReplayed: 100,
    });
    expect(r.projectedSavingsMicroUsd).toBe(0);
    expect(r.summary).toContain("no alternative candidate");
  });
});
