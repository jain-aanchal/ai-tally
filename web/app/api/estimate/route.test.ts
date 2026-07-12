// SPDX-License-Identifier: Apache-2.0
// Route tests for /api/estimate POST — body-driven what-if + honest-null floor (CTO-128).

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/clickhouse", () => ({
  queryReplayCandidates: vi.fn(),
  queryReplayEstimate: vi.fn(),
}));

import { POST as EstimatePOST } from "./route";
import * as ch from "@/lib/clickhouse";

const queryReplayEstimate = ch.queryReplayEstimate as unknown as ReturnType<typeof vi.fn>;

function postReq(body: unknown) {
  return new Request("http://test/api/estimate", {
    method: "POST",
    body: JSON.stringify(body),
  }) as never;
}

afterEach(() => vi.clearAllMocks());

describe("POST /api/estimate", () => {
  it("400s when candidateModel is missing", async () => {
    const res = await EstimatePOST(postReq({ systemPromptOverride: "x" }));
    expect(res.status).toBe(400);
  });

  it("maps a well-grounded projection into the proposed shape", async () => {
    queryReplayEstimate.mockResolvedValueOnce({
      samples_available: 120,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 5_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 60,
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 1000,
      },
    });

    const res = await EstimatePOST(
      postReq({ candidateModel: "claude-haiku-4-5", systemPromptOverride: "tighter prompt" }),
    );
    const body = await res.json();
    expect(body.replay_source).toBe("replay");
    expect(body.proposed.monthlyCostMicroUsd).toBe(5_000_000);
    expect(body.proposed.p99CostMicroUsd).toBe(Math.round(5_000_000 * 1.4));
    expect(body.proposed.meanLatencyMs).toBe(800);
    expect(body.groundedSamples).toBe(60);
    expect(body.candidate).toEqual({ provider: "anthropic", model: "claude-haiku-4-5" });

    // The override + candidate were forwarded to the gateway helper.
    expect(queryReplayEstimate).toHaveBeenCalledWith(
      expect.objectContaining({
        candidateModel: { provider: "anthropic", model: "claude-haiku-4-5" },
        systemPromptOverride: "tighter prompt",
      }),
    );
  });

  it("applies the honest-null floor when fewer than 50 samples ground the estimate", async () => {
    queryReplayEstimate.mockResolvedValueOnce({
      samples_available: 40,
      per_candidate: [
        {
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 5_000_000,
          p50_latency_ms: 800,
          p95_latency_ms: 1500,
          error_rate: 0.01,
          samples_replayed: 40, // below the 50-sample floor
          excluded_budget_count: 0,
        },
      ],
      diagnostics: {
        context_fidelity: "resolved-context replay (no live retrieval)",
        replay_cost_micro_usd: 1000,
      },
    });

    const res = await EstimatePOST(postReq({ candidateModel: "claude-haiku-4-5" }));
    const body = await res.json();
    expect(body.replay_source).toBe("mock");
    expect(body.proposed.monthlyCostMicroUsd).toBeNull();
    expect(body.proposed.p99CostMicroUsd).toBeNull();
    expect(body.proposed.meanLatencyMs).toBeNull();
    expect(body.groundedSamples).toBe(40);
  });

  it("applies the honest-null floor when the gateway returns null (no corpus / unreachable)", async () => {
    queryReplayEstimate.mockResolvedValueOnce(null);
    const res = await EstimatePOST(postReq({ candidateModel: "gpt-5-mini", providerOverride: "openai" }));
    const body = await res.json();
    expect(body.proposed.monthlyCostMicroUsd).toBeNull();
    expect(body.groundedSamples).toBe(0);
    expect(body.replay_source).toBe("mock");
  });
});
