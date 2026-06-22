// SPDX-License-Identifier: Apache-2.0
// Types + fallback mock for the Cross-provider Compare workflow. Real candidate data comes
// from /v1/replay (CTO-113) for cost/latency/error and from /v1/eval (CTO-114) for
// qualityScore. This module's `comparison` value is the rescaled-mock fallback used by the
// /api/compare route when the gateway has no opted-in samples yet (or is unreachable). The
// CandidateMetrics / Comparison types are the wire shape for both branches.
//
// CTO-115: the `current` row's `latencyP95Ms` and `errorRate` are now derived from live
// otel_spans over the same 7-day window the cost query uses (see queryCurrentModel in
// clickhouse.ts). They carry `null` when the live window has fewer than 50 spans, and the
// page renders "—" in that case.
//
// CTO-123: candidate rows' `latencyP95Ms` / `errorRate` are now also grounded in real
// per-candidate replay (p95 latency + error rate over the replayed responses), with the same
// honest-null floor (null below 50 replayed responses → "—"). The numeric values on the mock
// candidates below are used only by the unreachable-gateway rescaled-mock fallback path.
//
// CTO-114: `qualityScore` is now `number | null`. Non-null only when a pairwise-LLM-judge
// eval pass has run and judged >= 10 samples for that candidate; the value is the
// candidate's win-rate (candidate_wins / non-error judgments) with a Wilson 95% CI in
// `qualityCi`. When no eval has run, or n < 10, the route returns `null` and the page
// renders "—" rather than ever fabricating a quality number.
//
// The numeric mocks in `comparison.current` below are the unreachable-gateway fallback (CI /
// fresh clones); that path keeps showing numbers, not nulls, for backwards compatibility.

import type { MicroUSD } from "./types";

export interface CandidateMetrics {
  /** display label, e.g. "claude-haiku-4.5" */
  model: string;
  provider: string;
  /** projected monthly cost at current traffic */
  monthlyCostMicroUsd: MicroUSD;
  /**
   * Pairwise-LLM-judge win rate (0..1) from CTO-114. `null` when no eval pass has judged
   * >= 10 samples for this candidate — the page renders "—" in that case. NEVER substitute a
   * mock when this is null; the ticket is explicit about that. Always `null` on the `current`
   * row (no judge pair when comparing a model to itself).
   */
  qualityScore: number | null;
  /** Wilson 95% CI on the win-rate (CTO-114). Present only when `qualityScore` is a number. */
  qualityCi?: { lo: number; hi: number };
  /**
   * p95 latency in milliseconds. `null` on the `current` row when the live 7-day window has
   * fewer than 50 spans, and on candidate rows when fewer than 50 responses were replayed
   * (rendered as "—" — CTO-115 / CTO-123).
   */
  latencyP95Ms: number | null;
  /** 0..1. `null` on the `current` row under the same low-sample suppression rule (CTO-115). */
  errorRate: number | null;
}

export interface Comparison {
  workload: string;        // e.g. "research_agent / production / last 7 days"
  current: CandidateMetrics;
  candidates: CandidateMetrics[];
  /** human-written-ish recommendation (the routing rule export hooks off this) */
  recommendation: {
    verdict: "switch" | "keep" | "mixed";
    summary: string;
    projectedSavingsMicroUsd: MicroUSD;
    projectedSavingsPct: number; // 0..1
  };
  diagnostics: {
    samplesReplayed: number;
    samplesAvailable: number;
    excludedRateLimited: number;
    replayCostMicroUsd: MicroUSD;
    contextFidelity: "resolved-context replay (no live retrieval)" | "live retrieval";
    /**
     * Minutes since the reconciler last trued-up the baseline traffic this comparison is built
     * from. A projection off a stale baseline must not be presented as fresh (CTO-80).
     */
    reconcilerLastRunMinutesAgo: number;
  };
}

export function deltaPct(current: number, candidate: number): number {
  if (current === 0) return 0;
  return (candidate - current) / current;
}

// Compare fixture for the research_agent workload — the dominant cost driver from cost.ts
// ($19.1K LLM spend on this workload alone over 30 days, ≈ $4.5K/week).
export const comparison: Comparison = {
  workload: "research_agent / production / last 7 days",
  current: {
    model: "claude-sonnet-4.5",
    provider: "anthropic",
    monthlyCostMicroUsd: 19_100_000_000, // $19,100/mo on this workload
    qualityScore: 0.941,
    qualityCi: { lo: 0.911, hi: 0.962 },
    latencyP95Ms: 2400,
    errorRate: 0.004,
  },
  // No qualityCi on candidates: a CI implies real eval data, which the mock-fallback path
  // doesn't have. The /api/compare route nulls qualityScore on these in the fallback path too
  // (CTO-114: never fabricate a quality number); the values here document the ideal shape only.
  candidates: [
    {
      model: "claude-haiku-4.5",
      provider: "anthropic",
      monthlyCostMicroUsd: 5_300_000_000, // ~72% cheaper than current
      qualityScore: 0.908,
      latencyP95Ms: 1800,
      errorRate: 0.006,
    },
    {
      model: "gpt-5-mini",
      provider: "openai",
      monthlyCostMicroUsd: 6_250_000_000, // ~67% cheaper
      qualityScore: 0.894,
      latencyP95Ms: 1600,
      errorRate: 0.009,
    },
    {
      model: "gemini-3-flash",
      provider: "google",
      monthlyCostMicroUsd: 4_490_000_000, // ~76% cheaper
      qualityScore: 0.871,
      latencyP95Ms: 1400,
      errorRate: 0.012,
    },
  ],
  recommendation: {
    verdict: "mixed",
    summary:
      "Route short prompts (<1k tokens) to haiku-4.5; keep current for long-context (>4k tokens). Projected quality delta -1.1pp at the projected mix, saves ~$12.2K/mo on this workload.",
    projectedSavingsMicroUsd: 12_200_000_000,
    projectedSavingsPct: 0.64,
  },
  diagnostics: {
    samplesReplayed: 4200,
    samplesAvailable: 87_400,
    excludedRateLimited: 312,
    replayCostMicroUsd: 42_300_000, // $42.30 spent replaying = ~0.2% of monthly workload spend
    contextFidelity: "resolved-context replay (no live retrieval)",
    reconcilerLastRunMinutesAgo: 18,
  },
};
