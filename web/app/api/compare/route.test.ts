// SPDX-License-Identifier: Apache-2.0
// Route tests for /api/compare — exercises both the replay-backed and mock-fallback branches
// (CTO-113).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the clickhouse module before importing the route so the route picks up our stubs.
vi.mock("@/lib/clickhouse", () => ({
  queryCurrentModel: vi.fn(),
  queryReplayCandidates: vi.fn(),
  queryEvalCandidates: vi.fn(),
}));

import { GET as CompareGET } from "./route";
import * as ch from "@/lib/clickhouse";

const queryCurrentModel = ch.queryCurrentModel as unknown as ReturnType<typeof vi.fn>;
const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;
const queryEvalCandidates = ch.queryEvalCandidates as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  // Default: no eval pass has run. CTO-114 tests below override per-case.
  queryEvalCandidates.mockResolvedValue(null);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("/api/compare", () => {
  it("falls back to mock when no live data and no replay samples", async () => {
    queryCurrentModel.mockResolvedValueOnce(null);
    queryReplayCandidates.mockResolvedValueOnce(null);

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.replay_source).toBe("mock");
    expect(body.candidates.length).toBeGreaterThan(0);
  });

  it("uses rescaled mock when live current exists but no replay samples", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 1_310_000,
      latencyP95Ms: 2400,
      errorRate: 0.004,
      sampleCount: 500,
    });
    queryReplayCandidates.mockResolvedValueOnce(null);

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.replay_source).toBe("mock");
    expect(body.current.model).toBe("claude-sonnet-4-5");
    // CTO-115: live p95/error spliced through.
    expect(body.current.latencyP95Ms).toBe(2400);
    expect(body.current.errorRate).toBeCloseTo(0.004, 6);
    // Candidate costs should be rescaled small (off the live $1.31/mo baseline), not the
    // original $1,780/mo mock value.
    expect(body.candidates[0].monthlyCostMicroUsd).toBeLessThan(2_000_000);
  });

  // CTO-115: n < 50 in the 7-day window → queryCurrentModel returns nulls for latency/error,
  // and the route surfaces them on the wire as null so the page can render "—".
  it("surfaces null latency/error when n < 50 (mock-fallback branch)", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 1_310_000,
      latencyP95Ms: null,
      errorRate: null,
      sampleCount: 12,
    });
    queryReplayCandidates.mockResolvedValueOnce(null);

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.current.latencyP95Ms).toBeNull();
    expect(body.current.errorRate).toBeNull();
  });

  it("uses replay candidates when /v1/replay returns samples", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 10_000_000,
      latencyP95Ms: 2400,
      errorRate: 0.004,
      sampleCount: 500,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 50,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 3_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 50,
          excluded_budget_count: 0,
        },
        {
          provider: "openai",
          model: "gpt-5-mini",
          projected_monthly_cost_micro_usd: 4_000_000,
          p50_latency_ms: 900,
          p95_latency_ms: 1700,
          error_rate: 0.02,
          samples_replayed: 50,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 12_500,
      },
    });

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.replay_source).toBe("replay");
    expect(body.current.model).toBe("claude-sonnet-4-5");
    expect(body.candidates).toHaveLength(2);
    // Costs come straight from the projection, not the rescaled mock.
    const haiku = body.candidates.find((c: { model: string }) => c.model === "claude-haiku-4-5");
    expect(haiku.monthlyCostMicroUsd).toBe(3_000_000);
    // Savings recomputed off the cheapest candidate.
    expect(body.recommendation.projectedSavingsMicroUsd).toBe(7_000_000);
    // Diagnostics reflect the real samples_available + replay cost.
    expect(body.diagnostics.samplesAvailable).toBe(50);
    expect(body.diagnostics.replayCostMicroUsd).toBe(12_500);
    expect(body.diagnostics.contextFidelity).toBe(
      "resolved-context replay (no live retrieval)",
    );
    // CTO-123: per-candidate p95 latency + error rate come straight from the projection
    // (both candidates have >= 50 replayed responses), not a borrowed mock.
    expect(haiku.latencyP95Ms).toBe(1500);
    expect(haiku.errorRate).toBeCloseTo(0.01, 6);
  });

  // CTO-123: a candidate with fewer than 50 replayed responses gets null latency/error —
  // the same honest-null floor the `current` row uses (CTO-115) — so the page renders "—"
  // rather than a noisy number or a borrowed mock.
  it("CTO-123: nulls per-candidate latency/error below the 50-replay floor", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 10_000_000,
      latencyP95Ms: 2400,
      errorRate: 0.004,
      sampleCount: 500,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 49,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 3_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 49, // below the 50-replay floor
          excluded_budget_count: 0,
        },
        {
          provider: "openai",
          model: "gpt-5-mini",
          projected_monthly_cost_micro_usd: 4_000_000,
          p50_latency_ms: 900,
          p95_latency_ms: 1700,
          error_rate: 0.02,
          samples_replayed: 50, // exactly at the floor — keeps real numbers
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 12_500,
      },
    });

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    const haiku = body.candidates.find((c: { model: string }) => c.model === "claude-haiku-4-5");
    const mini = body.candidates.find((c: { model: string }) => c.model === "gpt-5-mini");
    // Below the floor → honest null (page renders "—"). Cost is still real (separate rule).
    expect(haiku.latencyP95Ms).toBeNull();
    expect(haiku.errorRate).toBeNull();
    expect(haiku.monthlyCostMicroUsd).toBe(3_000_000);
    // At the floor → real numbers pass through.
    expect(mini.latencyP95Ms).toBe(1700);
    expect(mini.errorRate).toBeCloseTo(0.02, 6);
  });

  it("drops the current model from replay candidates to avoid model-vs-itself comparisons", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-haiku-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 1_000_000,
      latencyP95Ms: 1500,
      errorRate: 0.005,
      sampleCount: 100,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 30,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",  // same as current; should be filtered out
          projected_monthly_cost_micro_usd: 1_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0,
          samples_replayed: 30,
          excluded_budget_count: 0,
        },
        {
          provider: "openai",
          model: "gpt-4o-mini",
          projected_monthly_cost_micro_usd: 500_000,
          p50_latency_ms: 900,
          p95_latency_ms: 1700,
          error_rate: 0,
          samples_replayed: 30,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: { context_fidelity: "resolved-context replay (no live retrieval)", replay_cost_micro_usd: 0 },
    });
    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.candidates).toHaveLength(1);
    expect(body.candidates[0].model).toBe("gpt-4o-mini");
  });

  // --- CTO-114: eval-backed quality scores -------------------------------------------------

  it("CTO-114: surfaces real win_rate + Wilson CI when eval has judged >= 10 samples", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 10_000_000,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 50,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 3_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 50,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 12_500,
      },
    });
    queryEvalCandidates.mockResolvedValueOnce({
      samples_available: 50,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          samples_judged: 25,
          current_wins: 10,
          candidate_wins: 11,
          ties: 4,
          errors: 0,
          win_rate: 0.44,
          win_rate_ci_lo: 0.26,
          win_rate_ci_hi: 0.63,
          judge_cost_micro_usd: 50_000,
        },
      ],
      diagnostics: { judge_model: "claude-opus-4-8", rubric_version: "rubric-v1", judge_cost_micro_usd: 50_000 },
    });

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    const haiku = body.candidates.find((c: { model: string }) => c.model === "claude-haiku-4-5");
    expect(haiku.qualityScore).toBeCloseTo(0.44);
    expect(haiku.qualityCi).toEqual({ lo: 0.26, hi: 0.63 });
    // Current model is never paired against itself — quality is null.
    expect(body.current.qualityScore).toBeNull();
  });

  it("CTO-114: surfaces null when eval has judged FEWER than 10 samples — never fabricates", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 10_000_000,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 50,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 3_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 50,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 12_500,
      },
    });
    queryEvalCandidates.mockResolvedValueOnce({
      samples_available: 5,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          samples_judged: 5, // below the 10-sample floor
          current_wins: 2,
          candidate_wins: 2,
          ties: 1,
          errors: 0,
          win_rate: 0.4,
          win_rate_ci_lo: 0.12,
          win_rate_ci_hi: 0.77,
          judge_cost_micro_usd: 10_000,
        },
      ],
      diagnostics: { judge_model: "claude-opus-4-8", rubric_version: "rubric-v1", judge_cost_micro_usd: 10_000 },
    });

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    const haiku = body.candidates.find((c: { model: string }) => c.model === "claude-haiku-4-5");
    expect(haiku.qualityScore).toBeNull();
    expect(haiku.qualityCi).toBeUndefined();
  });

  it("CTO-114: surfaces null when eval has not run at all", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-sonnet-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 10_000_000,
    });
    queryReplayCandidates.mockResolvedValueOnce({
      samples_available: 50,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 3_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 50,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 12_500,
      },
    });
    // queryEvalCandidates returns null by default (beforeEach).

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.candidates[0].qualityScore).toBeNull();
    expect(body.current.qualityScore).toBeNull();
  });
});
