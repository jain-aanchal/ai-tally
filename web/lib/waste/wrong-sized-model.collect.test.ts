// SPDX-License-Identifier: Apache-2.0
// Collector tests for the wrong-sized-model detector (per-call basis, CTO-236). These lock the LIVE
// wiring of `collectWrongSizedModel` on the per-call basis that replaced the inert v1 contract.
//
// The old contract (CTO-227 review pass 2) asserted the collector ALWAYS returned [] because it needed
// an incumbent row inside `replay.per_candidate` that the gateway never projects. CTO-236 removes that
// dependency: each candidate's cost per call is `projected_monthly_cost_micro_usd / samples_available`
// (confirmed against infra/gateway/src/gateway/app.py, which builds the projection as
// `round(avg_cost_per_call * samples_available)`), and the incumbent's cost per call is measured
// DIRECTLY from its own real traffic via `queryIncumbentPerCall` (avg EstimatedCost over its chat
// spans). No incumbent replay row, no cross-id cost join. So on the REAL replay shape (incumbent absent
// from per_candidate) the collector now FIRES when a candidate is cheaper per call at no quality
// regression, and still returns [] honestly when any required signal (corpus / eval / incumbent
// traffic) is missing.
//
// The pure detector (`detectWrongSizedModel`) is unit-tested separately in wrong-sized-model.test.ts;
// this file pins the collector's reads and the read ORDER (replay first, then eval, then incumbent).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Partial mock: keep the real `micro` and `clampWindowDays`-adjacent helpers, stub only the reads.
// `tryLive` is stubbed to run its callback (so `queryIncumbentPerCall`'s body executes against a
// stubbed `rowsP`), and `rowsP` returns the incumbent's spend/call row.
vi.mock("@/lib/clickhouse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/clickhouse")>();
  return {
    ...actual,
    queryCurrentModel: vi.fn(),
    queryReplayCandidates: vi.fn(),
    queryEvalCandidates: vi.fn(),
    tryLive: vi.fn(),
    rowsP: vi.fn(),
  };
});

import { collectWrongSizedModel } from "./wrong-sized-model";
import * as ch from "@/lib/clickhouse";
import type { DimensionFilters } from "@/lib/filters";

const queryCurrentModel = ch.queryCurrentModel as unknown as ReturnType<typeof vi.fn>;
const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;
const queryEvalCandidates = ch.queryEvalCandidates as unknown as ReturnType<typeof vi.fn>;
const tryLive = ch.tryLive as unknown as ReturnType<typeof vi.fn>;
const rowsP = ch.rowsP as unknown as ReturnType<typeof vi.fn>;

const NO_FILTERS: DimensionFilters = {
  feature: [],
  model: [],
  layer: [],
  provider: [],
  account: [],
};

// Incumbent = gpt-4o (the current model).
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
// incumbent. haiku is present; gpt-4o (the incumbent) is deliberately ABSENT, exactly as the gateway
// returns it. samples_available = 200, haiku projected 2B -> per-call 10,000,000 micro-USD.
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

// Stub the incumbent per-call read: `queryIncumbentPerCall` runs inside `tryLive`, whose body calls
// `rowsP` -> [{ spend, calls }]. Real `micro` converts "20000" -> 20,000,000,000 micro-USD; 1000 calls
// -> per-call 20,000,000 micro-USD (twice the haiku candidate's 10,000,000, so haiku qualifies).
function incumbentTraffic(spend = "20000", calls = 1000) {
  tryLive.mockImplementation(async (fn: (db: unknown, tenant: string) => Promise<unknown>) =>
    fn({}, "local-dev"),
  );
  rowsP.mockResolvedValue([{ spend, calls }]);
}

beforeEach(() => {
  queryEvalCandidates.mockReset();
  queryReplayCandidates.mockReset();
  queryCurrentModel.mockReset();
  tryLive.mockReset();
  rowsP.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("collectWrongSizedModel per-call basis (CTO-236)", () => {
  it("FIRES on the real replay shape (incumbent absent from per_candidate) for a cheaper, quality-passing candidate", async () => {
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(evalCandidatesOnly());
    queryCurrentModel.mockResolvedValue(currentModel());
    incumbentTraffic();

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("wrong_sized_model");
    expect(f.evidence.incumbentModel).toBe("gpt-4o");
    expect(f.evidence.candidateModel).toBe("claude-haiku-4-5");
    expect(f.evidence.incumbentPerCall).toBe(20_000_000);
    expect(f.evidence.candidatePerCall).toBe(10_000_000);
    // windowSpend 20B rescaled by 10M/20M -> 10B, recoverable 10B.
    expect(f.recoverableMicroUsd).toBe(10_000_000_000);
    expect(f.windowSpendMicroUsd).toBe(20_000_000_000);
    // Per-call, resolved-context caveat carried on the reason.
    expect(f.reason).toContain("per call");
    expect(f.reason).toContain("resolved-context replay, no live retrieval");
  });

  it("returns [] and does NO further read when there is no captured corpus (samples_available <= 0)", async () => {
    // Replay first: null corpus short-circuits before eval / current-model / incumbent reads run.
    queryReplayCandidates.mockResolvedValue(null);
    queryEvalCandidates.mockResolvedValue(evalCandidatesOnly());
    queryCurrentModel.mockResolvedValue(currentModel());
    incumbentTraffic();

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
    // Honest, and cheap: no corpus means we never touched eval, current-model, or the incumbent read.
    expect(queryEvalCandidates).not.toHaveBeenCalled();
    expect(queryCurrentModel).not.toHaveBeenCalled();
    expect(rowsP).not.toHaveBeenCalled();
  });

  it("returns [] when there is no eval quality signal (below the judged floor / null)", async () => {
    // The demo-tenant path: replay corpus exists but no eval projection -> honest [].
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(null);
    queryCurrentModel.mockResolvedValue(currentModel());
    incumbentTraffic();

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });

  it("returns [] when the candidate is not cheaper per call than the incumbent", async () => {
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(evalCandidatesOnly());
    queryCurrentModel.mockResolvedValue(currentModel());
    // Incumbent per-call 5,000,000 < candidate 10,000,000 -> candidate is pricier, no finding.
    incumbentTraffic("5000", 1000);

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });

  it("returns [] when the incumbent has no traffic in the window (no per-call basis)", async () => {
    queryReplayCandidates.mockResolvedValue(replayCandidatesOnly());
    queryEvalCandidates.mockResolvedValue(evalCandidatesOnly());
    queryCurrentModel.mockResolvedValue(currentModel());
    incumbentTraffic("0", 0);

    const findings = await collectWrongSizedModel(30, NO_FILTERS);
    expect(findings).toEqual([]);
  });
});
