// SPDX-License-Identifier: Apache-2.0
// Mock data for the pre-deploy Estimate workflow (CTO-71/72/73).

import type { MicroUSD } from "./types";

export interface Projection {
  workload: string;
  pr?: { repo: string; number: number; title: string };
  current: {
    monthlyCostMicroUsd: MicroUSD;
    p99CostMicroUsd: MicroUSD;
    meanLatencyMs: number;
  };
  proposed: {
    monthlyCostMicroUsd: MicroUSD;
    p99CostMicroUsd: MicroUSD;
    meanLatencyMs: number;
  };
  /** Probability that p99 cost more than doubles under the change (0..1). The headline risk. */
  blowUpRisk: number;
  drivers: { delta: number; reason: string }[]; // delta in micro-USD/month
  sample: {
    used: number;
    tailWeighted: number;
    pathologicalIncluded: number;
    ciHalfWidthPct: number; // on p99
  };
  /**
   * Minutes since the reconciler last trued-up the historical window this projection samples.
   * An estimate built on a stale baseline must not be presented as fresh (CTO-80).
   */
  reconcilerLastRunMinutesAgo: number;
}

/**
 * What-if projection returned by `POST /api/estimate` (CTO-128). Same shape as {@link Projection}
 * except the `proposed` cost/latency fields may be `null`: when fewer than the grounding floor of
 * samples back the estimate, the route returns `null` rather than fabricate a number, and the page
 * renders `—`. `groundedSamples` carries how many replayed samples actually grounded it.
 */
export interface WhatIfProjection extends Omit<Projection, "proposed"> {
  proposed: {
    monthlyCostMicroUsd: MicroUSD | null;
    p99CostMicroUsd: MicroUSD | null;
    meanLatencyMs: number | null;
  };
  candidate: { provider: string; model: string };
  systemPromptOverride?: string;
  groundedSamples: number;
  replay_source: "replay" | "mock";
}

export function pctDelta(cur: number, prop: number | null): number | null {
  if (prop === null) return null;
  if (cur === 0) return 0;
  return (prop - cur) / cur;
}

export const projection: Projection = {
  workload: "research_agent / production / last 30 days",
  pr: { repo: "jain-aanchal/ai-tally", number: 1284, title: "agent: add web_fetch retries + reranker step" },
  current: {
    monthlyCostMicroUsd: 19_100_000_000, // matches the research_agent line in cost.ts featureRows
    p99CostMicroUsd: 4_870_000,
    meanLatencyMs: 1400,
  },
  proposed: {
    monthlyCostMicroUsd: 33_240_000_000, // +74% projected
    p99CostMicroUsd: 11_580_000,
    meanLatencyMs: 2100,
  },
  blowUpRisk: 0.42,
  drivers: [
    { delta: 11_320_000_000, reason: "longer system prompt (4.2k → 6.8k tokens)" },
    { delta: 4_160_000_000, reason: "new tool call in 60% of paths" },
    { delta: -1_340_000_000, reason: "cached input recapture (Sonnet prompt caching)" },
  ],
  sample: {
    used: 180,
    tailWeighted: 140,
    pathologicalIncluded: 18,
    ciHalfWidthPct: 0.18,
  },
  reconcilerLastRunMinutesAgo: 18,
};
