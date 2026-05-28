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
function point(date: string, base: number, vectorBoost = 1): CostDayPoint {
  return {
    date,
    byLayer: {
      llm: base * 0.55,
      vector: base * 0.14 * vectorBoost,
      tools: base * 0.13,
      compute: base * 0.12,
      embeddings: base * 0.04,
      egress: base * 0.02,
    },
  };
}

export const costSeries: CostSeries = {
  reconciledThrough: "2026-05-19",
  days: [
    point("2026-05-13", 950_000_000),
    point("2026-05-14", 980_000_000),
    point("2026-05-15", 1_020_000_000),
    point("2026-05-16", 1_010_000_000),
    point("2026-05-17", 1_050_000_000),
    point("2026-05-18", 1_080_000_000),
    point("2026-05-19", 1_120_000_000),
    point("2026-05-20", 1_160_000_000, 2.4), // hidden vector spike
    point("2026-05-21", 1_220_000_000, 3.0),
    point("2026-05-22", 1_260_000_000, 3.4),
    point("2026-05-23", 1_300_000_000, 3.6),
    point("2026-05-24", 1_340_000_000, 3.8),
    point("2026-05-25", 1_380_000_000, 4.0),
    point("2026-05-26", 1_400_000_000, 4.0),
  ],
};

export const featureRows: FeatureCostRow[] = [
  {
    feature: "research_agent",
    byLayer: { llm: 4_210_000_000, vector: 1_820_000_000, tools: 1_640_000_000, compute: 1_210_000_000, embeddings: 380_000_000, egress: 60_000_000 },
  },
  {
    feature: "inline_writer",
    byLayer: { llm: 2_100_000_000, vector: 80_000_000, tools: 120_000_000, compute: 310_000_000, embeddings: 30_000_000, egress: 10_000_000 },
  },
  {
    feature: "smart_search",
    byLayer: { llm: 1_900_000_000, vector: 240_000_000, tools: 310_000_000, compute: 290_000_000, embeddings: 100_000_000, egress: 10_000_000 },
  },
];

export const hiddenCostAlerts: HiddenCostAlert[] = [
  {
    severity: "warn",
    message:
      "Your vector DB cost grew 4× this month while LLM cost was flat — index size doubled May 20.",
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
