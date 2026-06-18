// SPDX-License-Identifier: Apache-2.0
// Types + fallback mock for the Cross-provider Compare workflow. Real candidate data comes
// from /v1/replay (CTO-113); this module's `comparison` value is the rescaled-mock fallback
// used by the /api/compare route when the gateway has no opted-in samples yet (or is
// unreachable). The CandidateMetrics / Comparison types are the wire shape for both branches.
//
// CTO-115: the `current` row's `latencyP95Ms` and `errorRate` are now derived from live
// otel_spans over the same 7-day window the cost query uses (see queryCurrentModel in
// clickhouse.ts). They carry `null` when the live window has fewer than 50 spans, and the
// page renders "—" in that case. Everything else on this row (qualityScore) and every
// candidate row stays mock until CTO-114 (eval harness) and the per-candidate
// CTO-113-extension land. The numeric mocks in `comparison.current` below are the unreachable-
// ClickHouse fallback (CI / fresh clones); that path keeps showing numbers, not nulls.

import type { MicroUSD } from "./types";

export interface CandidateMetrics {
  /** display label, e.g. "claude-haiku-4.5" */
  model: string;
  provider: string;
  /** projected monthly cost at current traffic */
  monthlyCostMicroUsd: MicroUSD;
  /** pass rate from default LLM-judge eval (0..1) */
  qualityScore: number;
  /**
   * p95 latency in milliseconds. `null` on the `current` row when the live 7-day window has
   * fewer than 50 spans (rendered as "—" — CTO-115). Candidate rows keep numeric mocks until
   * per-candidate replay latency lands.
   */
  latencyP95Ms: number | null;
  /** 0..1. `null` on the `current` row under the same low-sample suppression rule. */
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

export const comparison: Comparison = {
  workload: "research_agent / production / last 7 days",
  current: {
    model: "claude-sonnet-4.5",
    provider: "anthropic",
    monthlyCostMicroUsd: 6_420_000_000,
    qualityScore: 0.941,
    latencyP95Ms: 2400,
    errorRate: 0.004,
  },
  candidates: [
    {
      model: "claude-haiku-4.5",
      provider: "anthropic",
      monthlyCostMicroUsd: 1_780_000_000,
      qualityScore: 0.908,
      latencyP95Ms: 1800,
      errorRate: 0.006,
    },
    {
      model: "gpt-5-mini",
      provider: "openai",
      monthlyCostMicroUsd: 2_100_000_000,
      qualityScore: 0.894,
      latencyP95Ms: 1600,
      errorRate: 0.009,
    },
    {
      model: "gemini-3-flash",
      provider: "google",
      monthlyCostMicroUsd: 1_510_000_000,
      qualityScore: 0.871,
      latencyP95Ms: 1400,
      errorRate: 0.012,
    },
  ],
  recommendation: {
    verdict: "mixed",
    summary:
      "Route short prompts (<1k tokens) to haiku-4.5; keep current for long-context. Quality delta -1.1pp at projected mix.",
    projectedSavingsMicroUsd: 4_100_000_000,
    projectedSavingsPct: 0.64,
  },
  diagnostics: {
    samplesReplayed: 4200,
    samplesAvailable: 87_400,
    excludedRateLimited: 312,
    replayCostMicroUsd: 14_200_000,
    contextFidelity: "resolved-context replay (no live retrieval)",
    reconcilerLastRunMinutesAgo: 36,
  },
};
