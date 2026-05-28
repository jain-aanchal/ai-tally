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
}

export function pctDelta(cur: number, prop: number): number {
  if (cur === 0) return 0;
  return (prop - cur) / cur;
}

export const projection: Projection = {
  workload: "research_agent / production / last 30 days",
  pr: { repo: "jain-aanchal/ai-tally", number: 1284, title: "agent: add web_fetch retries + reranker step" },
  current: {
    monthlyCostMicroUsd: 6_420_000_000,
    p99CostMicroUsd: 3_400_000,
    meanLatencyMs: 1400,
  },
  proposed: {
    monthlyCostMicroUsd: 11_200_000_000,
    p99CostMicroUsd: 8_100_000,
    meanLatencyMs: 2100,
  },
  blowUpRisk: 0.42,
  drivers: [
    { delta: 3_800_000_000, reason: "longer system prompt (4.2k → 6.8k tokens)" },
    { delta: 1_400_000_000, reason: "new tool call in 60% of paths" },
    { delta: -400_000_000, reason: "cached input recapture" },
  ],
  sample: {
    used: 180,
    tailWeighted: 140,
    pathologicalIncluded: 18,
    ciHalfWidthPct: 0.18,
  },
};
