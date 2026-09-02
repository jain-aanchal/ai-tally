// SPDX-License-Identifier: Apache-2.0
// Collector tests for the wrong-sized-model detector (per-call basis CTO-236; per-feature incumbent
// CTO-238). These lock the LIVE wiring of `collectWrongSizedModel`, which now resolves the incumbent
// PER FEATURE rather than tenant-wide.
//
// The bug CTO-238 fixes: the collector resolved ONE tenant-wide dominant model (queryCurrentModel) and
// compared every feature's per-feature replay corpus + eval against it, pairing one feature's
// candidates against a global incumbent. It now reads per (FeatureTag, model) spend + calls in one
// grouped ClickHouse read, picks each feature's own dominant model as that feature's incumbent, and
// iterates the top-K features by spend, calling replay/eval per feature and pushing one scope each.
//
// Each candidate's cost per call is `projected_monthly_cost_micro_usd / samples_available`, and the
// incumbent's cost per call is measured from that feature's own dominant-model traffic (spend / calls
// from the grouped read). The pure detector (`detectWrongSizedModel`) is unit-tested separately in
// wrong-sized-model.test.ts; this file pins the collector's reads, the per-feature scoping, and the
// filter narrowing.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Partial mock: keep the real `micro` at the money boundary, stub only the reads. `tryLive` runs its
// callback (so `queryFeatureIncumbents`'s body executes against the stubbed `rowsP`), `rowsP` returns
// the grouped (feature, model) spend/call rows, and replay/eval are stubbed per feature.
vi.mock("@/lib/clickhouse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/clickhouse")>();
  return {
    ...actual,
    queryReplayCandidates: vi.fn(),
    queryEvalCandidates: vi.fn(),
    tryLive: vi.fn(),
    rowsPCached: vi.fn(),
  };
});

import { collectWrongSizedModel } from "./wrong-sized-model";
import * as ch from "@/lib/clickhouse";
import type { DimensionFilters } from "@/lib/filters";

const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;
const queryEvalCandidates = ch.queryEvalCandidates as unknown as ReturnType<typeof vi.fn>;
const tryLive = ch.tryLive as unknown as ReturnType<typeof vi.fn>;
const rowsP = ch.rowsPCached as unknown as ReturnType<typeof vi.fn>;

function filters(over: Partial<DimensionFilters> = {}): DimensionFilters {
  return { feature: [], model: [], layer: [], provider: [], account: [], agent: [], ...over };
}

// The grouped (FeatureTag, model) read the collector runs. research_agent's dominant model is
// claude-sonnet-4-5 (max spend); chatbot's is gpt-4o. Each dominant model: spend "20000"/"10000",
// 1000 calls -> per-call 20,000,000 / 10,000,000 micro-USD. A cheap secondary model row on
// research_agent exercises dominance (it must NOT win) and the feature-total ranking key.
function groupedRows() {
  return [
    { feature: "research_agent", model: "claude-sonnet-4-5", spend: "20000", calls: 1000 },
    { feature: "research_agent", model: "gpt-4o-mini", spend: "50", calls: 200 },
    { feature: "chatbot", model: "gpt-4o", spend: "10000", calls: 1000 },
  ];
}

// tryLive runs the callback; rowsP returns the grouped rows. `micro` converts "20000" ->
// 20,000,000,000 micro-USD (window spend), /1000 -> 20,000,000 per call.
function incumbents(rows: unknown[] = groupedRows()) {
  tryLive.mockImplementation(async (fn: (db: unknown, tenant: string) => Promise<unknown>) =>
    fn({}, "local-dev"),
  );
  rowsP.mockResolvedValue(rows);
}

// Per-feature replay: haiku is the cheaper candidate for research_agent (projected 2B / 200 ->
// per-call 10,000,000, half the sonnet incumbent); gpt-5-mini for chatbot (projected 1B / 200 ->
// 5,000,000, half the gpt-4o incumbent). Any other feature has no corpus (null).
function replayFor(feature?: string) {
  const cand = (provider: string, model: string, projected: number) => ({
    provider,
    model,
    projected_monthly_cost_micro_usd: projected,
    p50_latency_ms: 900,
    p95_latency_ms: 1800,
    error_rate: 0.01,
    samples_replayed: 120,
    excluded_budget_count: 0,
  });
  const map: Record<string, unknown> = {
    research_agent: {
      samples_available: 200,
      per_candidate: [cand("anthropic", "claude-haiku-4-5", 2_000_000_000)],
      diagnostics: { context_fidelity: "resolved-context", replay_cost_micro_usd: 1000 },
    },
    chatbot: {
      samples_available: 200,
      per_candidate: [cand("openai", "gpt-5-mini", 1_000_000_000)],
      diagnostics: { context_fidelity: "resolved-context", replay_cost_micro_usd: 1000 },
    },
  };
  return map[feature ?? ""] ?? null;
}

function evalFor(feature?: string) {
  const cand = (provider: string, model: string) => ({
    provider,
    model,
    samples_judged: 40,
    current_wins: 18,
    candidate_wins: 20,
    ties: 2,
    errors: 0,
    win_rate: 0.5,
    win_rate_ci_lo: 0.44,
    win_rate_ci_hi: 0.56,
    judge_cost_micro_usd: 100,
  });
  const map: Record<string, unknown> = {
    research_agent: {
      samples_available: 200,
      per_candidate: [cand("anthropic", "claude-haiku-4-5")],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    },
    chatbot: {
      samples_available: 200,
      per_candidate: [cand("openai", "gpt-5-mini")],
      diagnostics: { judge_model: "j", rubric_version: "1", judge_cost_micro_usd: 1 },
    },
  };
  return map[feature ?? ""] ?? null;
}

function wireCorpora() {
  queryReplayCandidates.mockImplementation(async (feature?: string) => replayFor(feature));
  queryEvalCandidates.mockImplementation(async (feature?: string) => evalFor(feature));
}

beforeEach(() => {
  queryEvalCandidates.mockReset();
  queryReplayCandidates.mockReset();
  tryLive.mockReset();
  rowsP.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("collectWrongSizedModel per-feature incumbent (CTO-238)", () => {
  it("emits one finding per feature, each scoped to its OWN incumbent (dominant model)", async () => {
    incumbents();
    wireCorpora();

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toHaveLength(2);

    const byFeature = new Map(findings.map((f) => [f.scopeValue, f]));

    const research = byFeature.get("research_agent")!;
    expect(research.scopeKind).toBe("feature");
    expect(research.evidence.incumbentModel).toBe("claude-sonnet-4-5"); // NOT the cheap gpt-4o-mini row
    expect(research.evidence.candidateModel).toBe("claude-haiku-4-5");
    expect(research.evidence.incumbentPerCall).toBe(20_000_000);
    expect(research.evidence.candidatePerCall).toBe(10_000_000);
    // window 20B rescaled by 10M/20M -> 10B, recoverable 10B.
    expect(research.recoverableMicroUsd).toBe(10_000_000_000);
    expect(research.windowSpendMicroUsd).toBe(20_000_000_000);
    expect(research.reason).toContain("on research_agent");

    const chatbot = byFeature.get("chatbot")!;
    expect(chatbot.scopeKind).toBe("feature");
    expect(chatbot.evidence.incumbentModel).toBe("gpt-4o");
    expect(chatbot.evidence.candidateModel).toBe("gpt-5-mini");
    expect(chatbot.evidence.incumbentPerCall).toBe(10_000_000);
    expect(chatbot.evidence.candidatePerCall).toBe(5_000_000);
    // window 10B rescaled by 5M/10M -> 5B, recoverable 5B.
    expect(chatbot.recoverableMicroUsd).toBe(5_000_000_000);
    expect(chatbot.reason).toContain("on chatbot");
  });

  it("skips a feature with no corpus/eval, still flags the ones that have both", async () => {
    incumbents();
    // research_agent has a corpus + eval; chatbot's replay comes back null (no captured corpus).
    queryReplayCandidates.mockImplementation(async (feature?: string) =>
      feature === "chatbot" ? null : replayFor(feature),
    );
    queryEvalCandidates.mockImplementation(async (feature?: string) => evalFor(feature));

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeValue).toBe("research_agent");
  });

  it("skips a feature whose eval has no judged candidate over the floor", async () => {
    incumbents();
    queryReplayCandidates.mockImplementation(async (feature?: string) => replayFor(feature));
    // chatbot's eval returns null (opted out / no judge pass); research_agent still fires.
    queryEvalCandidates.mockImplementation(async (feature?: string) =>
      feature === "chatbot" ? null : evalFor(feature),
    );

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeValue).toBe("research_agent");
  });

  it("narrows to a single selected feature (filters.feature)", async () => {
    incumbents();
    wireCorpora();

    const findings = await collectWrongSizedModel(30, filters({ feature: ["chatbot"] }));
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeValue).toBe("chatbot");
    // Only the selected feature's corpus is read; research_agent is never replayed.
    const replayed = queryReplayCandidates.mock.calls.map((c) => c[0]);
    expect(replayed).toEqual(["chatbot"]);
  });

  it("keeps only features whose incumbent is in filters.model", async () => {
    incumbents();
    wireCorpora();

    // gpt-4o is chatbot's incumbent; research_agent's incumbent (claude-sonnet-4-5) is filtered out.
    const findings = await collectWrongSizedModel(30, filters({ model: ["gpt-4o"] }));
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeValue).toBe("chatbot");
    expect(findings[0].evidence.incumbentModel).toBe("gpt-4o");
  });

  it("returns [] when there is no tagged chat traffic in the window", async () => {
    incumbents([]);
    wireCorpora();

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toEqual([]);
    // No incumbents -> no replay/eval reads at all.
    expect(queryReplayCandidates).not.toHaveBeenCalled();
    expect(queryEvalCandidates).not.toHaveBeenCalled();
  });

  it("returns [] when ClickHouse is unreachable (grouped incumbent read null)", async () => {
    // tryLive returns null (query threw) -> queryFeatureIncumbents null -> honest [].
    tryLive.mockResolvedValue(null);
    wireCorpora();

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toEqual([]);
    expect(queryReplayCandidates).not.toHaveBeenCalled();
  });

  it("returns [] when a feature's candidate is not cheaper per call than its incumbent", async () => {
    // research_agent incumbent per-call 5,000,000 (spend "5000"/1000) < haiku candidate 10,000,000.
    incumbents([{ feature: "research_agent", model: "claude-sonnet-4-5", spend: "5000", calls: 1000 }]);
    wireCorpora();

    const findings = await collectWrongSizedModel(30, filters());
    expect(findings).toEqual([]);
  });
});
