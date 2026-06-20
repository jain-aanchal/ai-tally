// SPDX-License-Identifier: Apache-2.0
// Mock data for the Cost workflow (CTO-65/66). Typed for the eventual API.

import type { MicroUSD, SpendByLayer } from "./types";

export interface CostDayPoint {
  date: string; // ISO yyyy-mm-dd
  byLayer: SpendByLayer;
}

export interface CostSeries {
  /** chronological points, oldest → newest */
  days: CostDayPoint[];
  /** Boundary: data on or before this date is reconciled; after is estimated. */
  reconciledThrough: string;
}

export interface FeatureCostRow {
  feature: string;
  byLayer: SpendByLayer;
}

export interface HiddenCostAlert {
  message: string;
  severity: "info" | "warn";
}

export const LAYERS = ["llm", "vector", "tools", "compute", "embeddings", "egress"] as const;
export type Layer = (typeof LAYERS)[number];

export const LAYER_COLORS: Record<Layer, string> = {
  llm: "#5e6ad2",      // accent
  vector: "#26b5ce",
  tools: "#4cb782",    // good
  compute: "#f2c94c",  // warn
  embeddings: "#bb87fc",
  egress: "#8a93a6",   // muted
};

export const LAYER_LABEL: Record<Layer, string> = {
  llm: "LLM",
  vector: "Vector DB",
  tools: "Tool calls",
  compute: "Compute",
  embeddings: "Embeddings",
  egress: "Egress",
};

export function totalForDay(p: CostDayPoint): MicroUSD {
  return LAYERS.reduce((sum, l) => sum + p.byLayer[l], 0);
}

// 14 days, oldest → newest. Reconciled through day 8 (index), estimated after.
// Proportions match the per-feature mix in featureRows below: LLM dominates (real ingest today),
// vector / tools / compute / embeddings / egress are smaller shares to surface the all-in story.
function point(date: string, base: number, vectorBoost = 1): CostDayPoint {
  return {
    date,
    byLayer: {
      llm: base * 0.73,
      vector: base * 0.13 * vectorBoost,
      tools: base * 0.08,
      compute: base * 0.045,
      embeddings: base * 0.013,
      egress: base * 0.004,
    },
  };
}

export const costSeries: CostSeries = {
  reconciledThrough: "2026-06-12",
  days: [
    point("2026-06-06", 1_420_000_000),
    point("2026-06-07", 1_510_000_000),
    point("2026-06-08", 1_580_000_000),
    point("2026-06-09", 1_610_000_000),
    point("2026-06-10", 1_660_000_000),
    point("2026-06-11", 1_720_000_000),
    point("2026-06-12", 1_750_000_000),
    point("2026-06-13", 1_790_000_000, 1.8), // vector index expanded, shows up immediately
    point("2026-06-14", 1_830_000_000, 2.0),
    point("2026-06-15", 1_890_000_000, 2.1),
    point("2026-06-16", 1_940_000_000, 2.2),
    point("2026-06-17", 1_980_000_000, 2.3),
    point("2026-06-18", 2_050_000_000, 2.3),
    point("2026-06-19", 2_170_000_000, 2.4),
  ],
};

// Per-feature 30-day spend, summing to ~$52,400 (matches mock.ts mockSpend.totalMicroUsd).
// research_agent is the dominant cost driver (~54%), classic story for an agentic startup.
export const featureRows: FeatureCostRow[] = [
  {
    feature: "research_agent",
    byLayer: { llm: 19_100_000_000, vector: 4_760_000_000, tools: 2_460_000_000, compute: 1_440_000_000, embeddings: 350_000_000, egress: 100_000_000 },
  },
  {
    feature: "support_triage",
    byLayer: { llm: 7_640_000_000, vector: 0, tools: 820_000_000, compute: 480_000_000, embeddings: 0, egress: 40_000_000 },
  },
  {
    feature: "inline_writer",
    byLayer: { llm: 5_730_000_000, vector: 0, tools: 410_000_000, compute: 240_000_000, embeddings: 0, egress: 0 },
  },
  {
    feature: "smart_search",
    byLayer: { llm: 3_820_000_000, vector: 1_360_000_000, tools: 0, compute: 0, embeddings: 210_000_000, egress: 0 },
  },
  {
    feature: "chatbot",
    byLayer: { llm: 1_910_000_000, vector: 680_000_000, tools: 410_000_000, compute: 240_000_000, embeddings: 140_000_000, egress: 60_000_000 },
  },
];

export const hiddenCostAlerts: HiddenCostAlert[] = [
  {
    severity: "warn",
    message:
      "research_agent vector cost grew 2.4× over the last 7 days while LLM cost grew 1.2×. Pinecone index expanded on June 13.",
  },
  {
    severity: "info",
    message:
      "support_triage averaged 3.2 LLM calls per session this week (up from 2.4). Worth checking the retry loop on tool.search_kb.",
  },
];

export function totalRange(series: CostSeries): MicroUSD {
  return series.days.reduce((s, d) => s + totalForDay(d), 0);
}

export function reconciledTotal(series: CostSeries): MicroUSD {
  return series.days
    .filter((d) => d.date <= series.reconciledThrough)
    .reduce((s, d) => s + totalForDay(d), 0);
}

export function estimatedTotal(series: CostSeries): MicroUSD {
  return series.days
    .filter((d) => d.date > series.reconciledThrough)
    .reduce((s, d) => s + totalForDay(d), 0);
}
