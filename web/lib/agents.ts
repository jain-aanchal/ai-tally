// SPDX-License-Identifier: Apache-2.0
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

/**
 * Minutes since the reconciler last trued-up these per-agent cost numbers. Agents presents
 * reconciled cost (cost/day, p50/p99), so it carries the same freshness signal as Cost/Features
 * and must surface staleness the same way (CTO-80, "never show stale as fresh").
 */
export const RECONCILER_LAST_RUN_MINUTES_AGO = 23;

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

// 30-day cost-per-day totals here are derived from featureRows in cost.ts so the views agree:
// research_agent $28,210 / 30 ≈ $940/day, support_triage $8,980 / 30 ≈ $299/day, etc.
export const agents: AgentSummary[] = [
  {
    name: "research_agent",
    runsPerDay: 1240,
    costPerDayMicroUsd: 940_000_000,
    p50MicroUsd: 120_000,
    p99MicroUsd: 4_870_000,
    distribution: [40, 120, 260, 180, 90, 40, 18, 8, 4, 2],
  },
  {
    name: "support_triage",
    runsPerDay: 8300,
    costPerDayMicroUsd: 299_000_000,
    p50MicroUsd: 30_000,
    p99MicroUsd: 220_000,
    distribution: [180, 580, 430, 190, 80, 28, 10, 4, 1, 0],
  },
  {
    name: "inline_writer",
    runsPerDay: 42_100,
    costPerDayMicroUsd: 213_000_000,
    p50MicroUsd: 4_000,
    p99MicroUsd: 18_000,
    distribution: [820, 1350, 760, 260, 90, 26, 8, 3, 0, 0],
  },
  {
    name: "chatbot",
    runsPerDay: 6800,
    costPerDayMicroUsd: 115_000_000,
    p50MicroUsd: 12_000,
    p99MicroUsd: 95_000,
    distribution: [140, 460, 380, 170, 65, 22, 9, 3, 1, 0],
  },
  {
    name: "smart_search",
    runsPerDay: 14_200,
    costPerDayMicroUsd: 180_000_000,
    p50MicroUsd: 8_000,
    p99MicroUsd: 890_000,
    distribution: [310, 920, 540, 220, 80, 28, 14, 6, 2, 1],
  },
];

export const runs: AgentRun[] = [
  {
    runId: "research_run_8af2",
    agent: "research_agent",
    totalCostMicroUsd: 4_870_000,
    multipleOfMedian: 41,
    steps: 21,
    outcome: "success",
    whyExpensive:
      "41× median: a 14-step retry loop on tool.web_fetch after a 429, then a 24k-token synthesize/refine pair (87% of total cost).",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 4_870_000, durationMs: 11200, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.plan", costMicroUsd: 14_000, durationMs: 740, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.web_fetch (×14, 429 backoff)", costMicroUsd: 18_000, durationMs: 6900, status: "retry" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.synthesize (16k ctx)", costMicroUsd: 2_310_000, durationMs: 1450, status: "ok" },
      { spanId: "s4", parentSpanId: "s0", name: "llm.refine (24k ctx)", costMicroUsd: 2_528_000, durationMs: 1110, status: "ok" },
    ],
  },
  {
    runId: "research_run_3c01",
    agent: "research_agent",
    totalCostMicroUsd: 3_120_000,
    multipleOfMedian: 26,
    steps: 12,
    outcome: "failed",
    whyExpensive:
      "26× median: tool fan-out called search_docs 9× before the model gave up. Failed run still incurred full retrieval + synthesis cost.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 3_120_000, durationMs: 8400, status: "error" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.plan", costMicroUsd: 11_000, durationMs: 600, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.search_docs (×9)", costMicroUsd: 9_000, durationMs: 3400, status: "ok" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.synthesize (28k ctx)", costMicroUsd: 3_100_000, durationMs: 2200, status: "error" },
    ],
  },
  {
    runId: "research_run_7b3a",
    agent: "research_agent",
    totalCostMicroUsd: 760_000,
    multipleOfMedian: 6,
    steps: 8,
    outcome: "success",
    whyExpensive:
      "6× median: standard multi-source synthesis; within normal range but on the high end of acceptable.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 760_000, durationMs: 4800, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.plan", costMicroUsd: 10_000, durationMs: 540, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.web_fetch (×3)", costMicroUsd: 6_000, durationMs: 1900, status: "ok" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.synthesize (12k ctx)", costMicroUsd: 744_000, durationMs: 1100, status: "ok" },
    ],
  },
  {
    runId: "search_run_99fb",
    agent: "smart_search",
    totalCostMicroUsd: 890_000,
    multipleOfMedian: 22,
    steps: 5,
    outcome: "success",
    whyExpensive:
      "22× median: an unusually large query embedded against the full vector index, then re-ranked with a frontier model.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 890_000, durationMs: 2400, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "tool.embed_query (3.2k tokens)", costMicroUsd: 8_000, durationMs: 220, status: "ok" },
      { spanId: "s2", parentSpanId: "s0", name: "tool.vector_search (top-100)", costMicroUsd: 12_000, durationMs: 480, status: "ok" },
      { spanId: "s3", parentSpanId: "s0", name: "llm.rerank (opus-4)", costMicroUsd: 870_000, durationMs: 1700, status: "ok" },
    ],
  },
  {
    runId: "writer_run_2f1c",
    agent: "inline_writer",
    totalCostMicroUsd: 540_000,
    multipleOfMedian: 14,
    steps: 3,
    outcome: "success",
    whyExpensive:
      "14× median for this agent: 11k-token user document with full rewrite. Unusual for inline_writer (typical < 1k input).",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 540_000, durationMs: 1850, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.rewrite (sonnet-4.5, 11k ctx)", costMicroUsd: 540_000, durationMs: 1820, status: "ok" },
    ],
  },
  {
    runId: "support_run_5d77",
    agent: "support_triage",
    totalCostMicroUsd: 220_000,
    multipleOfMedian: 7,
    steps: 4,
    outcome: "success",
    whyExpensive: "7× median: a single long-context classification; within normal range for this agent.",
    spans: [
      { spanId: "s0", parentSpanId: null, name: "agent.run", costMicroUsd: 220_000, durationMs: 1400, status: "ok" },
      { spanId: "s1", parentSpanId: "s0", name: "llm.classify (8k ctx)", costMicroUsd: 220_000, durationMs: 1350, status: "ok" },
    ],
  },
];

export function runsForAgent(agent: string): AgentRun[] {
  return runs.filter((r) => r.agent === agent);
}

export function getRun(runId: string): AgentRun | undefined {
  return runs.find((r) => r.runId === runId);
}
