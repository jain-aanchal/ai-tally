// SPDX-License-Identifier: Apache-2.0
// Collector tests for the wrong-sized-model detector (CTO-227 review findings, Bugs 1 & 4). Unlike
// the pure-detector suite, these mock the Compare-path ClickHouse reads to lock the WIRING the two
// bugs live in:
//   * Bug 1: the rescale denominator is the incumbent's REPLAY-corpus projection, the same basis the
//     candidates use — never the 7-day-traffic projection. A candidate cheaper only because of the
//     old basis mismatch does NOT flag; and when the incumbent has no replay entry (so the sides
//     cannot be compared apples-to-apples) the collector returns NOTHING, honestly.
//   * Bug 4: the incumbent id joins a cost-breakdown row (both resolved response-model-first), so the
//     detector actually fires instead of silently returning [] on a key mismatch.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/clickhouse", () => ({
  queryCurrentModel: vi.fn(),
  queryReplayCandidates: vi.fn(),
  queryEvalCandidates: vi.fn(),
  queryCostExplore: vi.fn(),
}));

import { collectWrongSizedModel } from "./wrong-sized-model";
import * as ch from "@/lib/clickhouse";
import type { DimensionFilters } from "@/lib/filters";

const queryCurrentModel = ch.queryCurrentModel as unknown as ReturnType<typeof vi.fn>;
const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;
const queryEvalCandidates = ch.queryEvalCandidates as unknown as ReturnType<typeof vi.fn>;
const queryCostExplore = ch.queryCostExplore as unknown as ReturnType<typeof vi.fn>;

const NO_FILTERS: DimensionFilters = {
  feature: [],
  model: [],
  layer: [],
  provider: [],
  account: [],
};

// Incumbent = gpt-4o. Its replay projection is 8B micro-USD/mo; the candidate haiku replays at 2B on
// the SAME corpus, so it is genuinely half-of-a-quarter cheaper and qualifies. Window spend is 20B.
function currentModel(over: Record<string, unknown> = {}) {
  return {
    model: "gpt-4o",
    provider: "openai",
    // Deliberately DIFFERENT basis/scale from the replay projection: this is the 7-day-traffic figure
    // Bug 1 must NOT use as the denominator. If it leaked back in, the recoverable math would break.
    monthlyCostMicroUsd: 40_000_000_000,
    latencyP95Ms: 2400,
    errorRate: 0.01,
    sampleCount: 500,
    ...over,
  };
}

function replay(perCandidate: Array<Record<string, unknown>>) {
  return {
    samples_available: 200,
    per_candidate: perCandidate,
    diagnostics: { context_fidelity: "resolved-context", replay_cost_micro_usd: 1000 },
  };
}

function replayRow(over: Record<string, unknown> = {}) {
  return {
    provider: "openai",
    model: "gpt-4o",
    projected_monthly_cost_micro_usd: 8_000_000_000,
    p50_latency_ms: 900,
    p95_latency_ms: 1800,
    error_rate: 0.01,
    samples_replayed: 120,
    excluded_budget_count: 0,
    ...over,
  };
}

function evalRow(over: Record<string, unknown> = {}) {
  return {
    provider: "anthropic",
    model: "claude-haiku-4-5",
    samples_judged: 40,
    current_wins: 18,
    candidate_wins: 20,
    ties: 2,
    errors: 0,
    win_rate: 0.5,
    win_rate_ci_lo: 0.44,
    win_rate_ci_hi: 0.56,
    judge_cost_micro_usd: 100,
    ...over,
  };
}

function costExplore(breakdown: Array<{ group: string; totalMicroUsd: number }>) {
  return {
    groupBy: "model" as const,
    windowStart: "2026-08-01",
    windowEnd: "2026-08-26",
    windowDays: 30,
    groups: breakdown.map((b) => b.group),
    days: [],
    breakdown: breakdown.map((b) => ({ ...b, spanCount: 100 })),
    totalMicroUsd: breakdown.reduce((s, b) => s + b.totalMicroUsd, 0),
    truncatedGroups: 0,
  };
}

beforeEach(() => {
  queryEvalCandidates.mockReset();
  queryReplayCandidates.mockReset();
  queryCurrentModel.mockReset();
  queryCostExplore.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("collectWrongSizedModel — replay-basis rescale (CTO-227 Bug 1) + id join (Bug 4)", () => {
  it("flags a candidate cheaper on the shared replay basis with the correct recoverable", async () => {
    queryCurrentModel.mockResolvedValue(currentModel());
    // Replay set includes the incumbent (gpt-4o @ 8B) AND a cheaper candidate (haiku @ 2B), same basis.
    queryReplayCandidates.mockResolvedValue(
      replay([
        replayRow({ provider: "openai", model: "gpt-4o", projected_monthly_cost_micro_usd: 8_000_000_000 }),
        replayRow({
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 2_000_000_000,
        }),
      ]),
    );
    queryEvalCandidates.mockResolvedValue({
      samples_available: 200,
      per_candidate: [evalRow()],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    });
    // Cost breakdown keyed by the SAME response-model-first id the incumbent resolves to.
    queryCostExplore.mockResolvedValue(costExplore([{ group: "gpt-4o", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("wrong_sized_model");
    expect(f.scopeValue).toBe("gpt-4o");
    expect(f.evidence.candidateModel).toBe("claude-haiku-4-5");
    // ratio = 2B/8B = 0.25 of the 20B window spend rescales to 5B, recoverable = 15B. NOT computed
    // off the 40B traffic projection (which would have made the ratio 0.05 and the recoverable absurd).
    expect(f.recoverableMicroUsd).toBe(15_000_000_000);
    expect(f.windowSpendMicroUsd).toBe(20_000_000_000);
  });

  it("does NOT flag a candidate that is cheaper only against the traffic projection (basis mismatch)", async () => {
    queryCurrentModel.mockResolvedValue(currentModel());
    // On the replay basis the candidate (10B) is MORE expensive than the incumbent (8B). It would only
    // look "cheaper" if compared against the incumbent's 40B traffic projection — the exact Bug 1 trap.
    queryReplayCandidates.mockResolvedValue(
      replay([
        replayRow({ provider: "openai", model: "gpt-4o", projected_monthly_cost_micro_usd: 8_000_000_000 }),
        replayRow({
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 10_000_000_000,
        }),
      ]),
    );
    queryEvalCandidates.mockResolvedValue({
      samples_available: 200,
      per_candidate: [evalRow()],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    });
    queryCostExplore.mockResolvedValue(costExplore([{ group: "gpt-4o", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });

  it("returns NOTHING when the incumbent has no replay entry (cannot compare apples-to-apples)", async () => {
    queryCurrentModel.mockResolvedValue(currentModel());
    // Replay set does NOT include the incumbent gpt-4o, only a candidate. No shared basis -> honest [].
    queryReplayCandidates.mockResolvedValue(
      replay([
        replayRow({
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 2_000_000_000,
        }),
      ]),
    );
    queryEvalCandidates.mockResolvedValue({
      samples_available: 200,
      per_candidate: [evalRow()],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    });
    queryCostExplore.mockResolvedValue(costExplore([{ group: "gpt-4o", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });

  it("returns [] when the incumbent id matches no cost-breakdown row", async () => {
    queryCurrentModel.mockResolvedValue(currentModel({ model: "gpt-4o" }));
    queryReplayCandidates.mockResolvedValue(
      replay([
        replayRow({ provider: "openai", model: "gpt-4o", projected_monthly_cost_micro_usd: 8_000_000_000 }),
        replayRow({
          provider: "anthropic",
          model: "claude-haiku-4-5",
          projected_monthly_cost_micro_usd: 2_000_000_000,
        }),
      ]),
    );
    queryEvalCandidates.mockResolvedValue({
      samples_available: 200,
      per_candidate: [evalRow()],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    });
    // Breakdown keyed by a DIFFERENT id than the incumbent resolves to -> no join, no finding.
    queryCostExplore.mockResolvedValue(costExplore([{ group: "some-other-model", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });
});
