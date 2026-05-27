// Mock data + helpers for the Agents workflow (CTO-55/56/57). Typed like the eventual API.

import type { MicroUSD } from "./types";

export interface AgentSummary {
  name: string;
  runsPerDay: number;
  costPerDayMicroUsd: MicroUSD;
  p50MicroUsd: MicroUSD;
  p99MicroUsd: MicroUSD;
  /** log-scale distribution buckets (counts), cheap → expensive */
  distribution: number[];
}

export function p99Ratio(a: AgentSummary): number {
  return a.p50MicroUsd === 0 ? 0 : a.p99MicroUsd / a.p50MicroUsd;
}

export interface RunSpan {
  spanId: string;
  parentSpanId: string | null;
  name: string;
  costMicroUsd: MicroUSD;
  durationMs: number;
  status: "ok" | "retry" | "error";
}

export interface AgentRun {
  runId: string;
  agent: string;
  totalCostMicroUsd: MicroUSD;
  multipleOfMedian: number;
  steps: number;
  outcome: "success" | "failed" | "abandoned";
  /** one-sentence auto-generated root cause (CTO-57) */
  whyExpensive: string;
  spans: RunSpan[];
}

export const agents: AgentSummary[] = [
  {
    name: "research_agent",
    runsPerDay: 1240,
    costPerDayMicroUsd: 214_000_000,
    p50MicroUsd: 120_000,
    p99MicroUsd: 3_400_000,
    distribution: [40, 120, 260, 180, 90, 40, 18, 8, 4, 2],
  },
  {
    name: "support_triage",
    runsPerDay: 8300,
    costPerDayMicroUsd: 180_000_000,
    p50MicroUsd: 20_000,
    p99MicroUsd: 140_000,
    distribution: [200, 620, 410, 160, 60, 20, 8, 3, 1, 0],
  },
  {
    name: "inline_writer",
    runsPerDay: 42_100,
    costPerDayMicroUsd: 85_000_000,
    p50MicroUsd: 2_000,
    p99MicroUsd: 10_000,
    distribution: [900, 1400, 700, 220, 70, 20, 6, 2, 0, 0],
  },
];

export const runs: AgentRun[] = [
  {
    runId: "research_run_8af2",
    agent: "research_agent",
    totalCostMicroUsd: 3_400_000,
    multipleOfMedian: 78,
    steps: 21,
    outcome: "success",
    whyExpensive:
      "47× median due to a 14-step retry loop on tool web_fetch after a 429, then a 24k-token synthesize/refine pair (87% of cost).",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 3_400_000, durationMs: 10040, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.plan", costMicroUsd: 12_000, durationMs: 740, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.web_fetch (×14, 429 backoff)", costMicroUsd: 14_000, durationMs: 6200, status: "retry" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.synthesize (16k ctx)", costMicroUsd: 1_840_000, durationMs: 1300, status: "ok" },
      { spanId: "s4", parentSpanId: "s0", name: "llm.refine (24k ctx)", costMicroUsd: 1_420_000, durationMs: 980, status: "ok" },
    ],
  },
  {
    runId: "research_run_3c01",
    agent: "research_agent",
    totalCostMicroUsd: 1_920_000,
    multipleOfMedian: 44,
    steps: 12,
    outcome: "failed",
    whyExpensive:
      "44× median: tool fan-out called search_docs 9× before the model gave up; failed run still incurred full retrieval cost.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 1_920_000, durationMs: 7100, status: "error" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.plan", costMicroUsd: 11_000, durationMs: 600, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.search_docs (×9)", costMicroUsd: 9_000, durationMs: 3400, status: "ok" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.synthesize", costMicroUsd: 1_900_000, durationMs: 1900, status: "error" },
    ],
  },
  {
    runId: "support_run_5d77",
    agent: "support_triage",
    totalCostMicroUsd: 140_000,
    multipleOfMedian: 7,
    steps: 4,
    outcome: "success",
    whyExpensive: "7× median: a single long-context classification; within normal range for this agent.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 140_000, durationMs: 1200, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.classify (8k ctx)", costMicroUsd: 140_000, durationMs: 1100, status: "ok" },
    ],
  },
];

export function runsForAgent(agent: string): AgentRun[] {
  return runs.filter((r) => r.agent === agent);
}

export function getRun(runId: string): AgentRun | undefined {
  return runs.find((r) => r.runId === runId);
}
