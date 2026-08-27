// SPDX-License-Identifier: Apache-2.0
// Collector tests for the wrong-sized-model detector (CTO-227 review pass 2). These lock the HONEST
// v1 contract of `collectWrongSizedModel`, replacing an earlier suite that asserted the detector
// FIRING via an incumbent row mocked into `replay.per_candidate`, a shape the real gateway NEVER
// returns.
//
// CONFIRMED against gateway source (infra/gateway/src/gateway/app.py, the /v1/replay per-candidate
// loop) and `queryReplayCandidates` (web/lib/clickhouse.ts): v1 `/v1/replay` builds `per_candidate`
// ONLY from the requested `candidate_models`, and `queryReplayCandidates` sends just DEFAULT_CANDIDATES
// (claude-haiku-4-5, gpt-5-mini, gemini-3-flash). The incumbent is never projected. So the collector's
// denominator lookup `replay.per_candidate.find(r => r.model === incumbentModel)` is ALWAYS undefined
// for a real incumbent, and the collector returns [] every time in production. That is deliberate
// honesty until the per-call-cost rescale lands in CTO-236, not a masked bug. These tests therefore
// assert [] on the REAL replay shape (incumbent absent from per_candidate); a test that encoded firing
// off a mocked incumbent row would encode a contract the gateway does not honor.
//
// The pure detector (`detectWrongSizedModel`) remains legitimately unit-tested in
// wrong-sized-model.test.ts: it is correct GIVEN a valid input. This file only pins the collector's
// wiring, where no such input can be produced on v1.

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

// Incumbent = gpt-4o (the current model). Its window spend is 20B micro-USD.
function currentModel(over: Record<string, unknown> = {}) {
  return {
    model: "gpt-4o",
    provider: "openai",
    monthlyCostMicroUsd: 40_000_000_000,
    latencyP95Ms: 2400,
    errorRate: 0.01,
    sampleCount: 500,
    ...over,
  };
}

// The REAL replay shape: per_candidate carries ONLY the requested candidate models, never the
// incumbent. Here the cheaper, quality-passing candidate haiku is present; gpt-4o (the incumbent) is
// deliberately ABSENT, exactly as the gateway returns it.
function replayCandidatesOnly() {
  return {
    samples_available: 200,
    per_candidate: [
      {
        provider: "anthropic",
        model: "claude-haiku-4-5",
        projected_monthly_cost_micro_usd: 2_000_000_000,
        p50_latency_ms: 900,
        p95_latency_ms: 1800,
        error_rate: 0.01,
        samples_replayed: 120,
        excluded_budget_count: 0,
      },
    ],
    diagnostics: { context_fidelity: "resolved-context", replay_cost_micro_usd: 1000 },
  };
}

function evalCandidatesOnly() {
  return {
    samples_available: 200,
    per_candidate: [
      {
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
      },
    ],
    diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
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

describe("collectWrongSizedModel honest v1 contract (CTO-227 review pass 2; real impl CTO-236)", () => {
  it("returns [] on the real replay shape (incumbent absent from per_candidate), even with a cheaper, quality-passing candidate", async () => {
    // Every read is present and healthy, and haiku is genuinely cheaper on the replay corpus and passes
    // the quality floor. It STILL yields nothing, because v1 /v1/replay never projects the incumbent,
    // so the rescale denominator cannot be formed. This is the production reality until CTO-236.
    queryCurrentModel.mockResolvedValue(currentModel());
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(evalCandidatesOnly());
    queryCostExplore.mockResolvedValue(costExplore([{ group: "gpt-4o", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    // Honest []: NOT a firing finding produced by a mocked incumbent row the gateway never returns.
    expect(findings).toEqual([]);
  });

  it("returns [] when a required read is unavailable (no eval pass)", async () => {
    // The demo-tenant path: no eval projection -> nothing to compare on quality -> honest [].
    queryCurrentModel.mockResolvedValue(currentModel());
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(null);
    queryCostExplore.mockResolvedValue(costExplore([{ group: "gpt-4o", totalMicroUsd: 20_000_000_000 }]));

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });
});
