// SPDX-License-Identifier: Apache-2.0
// Route tests for /api/compare — exercises both the replay-backed and mock-fallback branches
// (CTO-113).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the clickhouse module before importing the route so the route picks up our stubs.
vi.mock("@/lib/clickhouse", () => ({
  queryCurrentModel: vi.fn(),
  queryReplayCandidates: vi.fn(),
}));

import { GET as CompareGET } from "./route";
import * as ch from "@/lib/clickhouse";

const queryCurrentModel = ch.queryCurrentModel as unknown as ReturnType<typeof vi.fn>;
const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;

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
    });
    queryReplayCandidates.mockResolvedValueOnce(null);

    const res = await CompareGET(new Request("http://test/api/compare") as never);
    const body = await res.json();
    expect(body.replay_source).toBe("mock");
    expect(body.current.model).toBe("claude-sonnet-4-5");
    // Candidate costs should be rescaled small (off the live $1.31/mo baseline), not the
    // original $1,780/mo mock value.
    expect(body.candidates[0].monthlyCostMicroUsd).toBeLessThan(2_000_000);
  });

  it("uses replay candidates when /v1/replay returns samples", async () => {
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
  });

  it("drops the current model from replay candidates to avoid model-vs-itself comparisons", async () => {
    queryCurrentModel.mockResolvedValueOnce({
      model: "claude-haiku-4-5",
      provider: "anthropic",
      monthlyCostMicroUsd: 1_000_000,
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
});
