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
  llm: "#5e81ac",      // accent
  vector: "#26b5ce",
  tools: "#4c9f70",    // good
  compute: "#cc9a1f",  // warn
  embeddings: "#bb87fc",
  egress: "#4c566a",   // muted
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

/** One layer's figure for a window, or the reason we cannot report one. */
export interface LayerCoverage {
  layer: Layer;
  /** Measured spend in micro-USD, or null when we have nothing to report for this window. */
  totalMicroUsd: MicroUSD | null;
  /** Why it is blank. Empty string exactly when totalMicroUsd is a real measured number. */
  reason: string;
}

/**
 * Classify every cost layer for a window (CTO-244).
 *
 * LAYERS is a fixed six-element list of the layers the product knows about, not a list of the
 * layers a given tenant has data for. Mapping it straight onto a per-layer sum therefore invented a
 * row for a layer nobody ever reported, and that row read "Compute $0.00, 0.0%": a confident zero
 * for something we never measured. Same failure as a fabricated cost, on the read side.
 *
 * Telling "genuinely spent nothing" from "we have nothing" needs a second signal, and there are two,
 * in order of strength:
 *   - `spanCounts`: spans we actually observed for the layer. A layer with spans summing to 0 IS a
 *     measured zero, and it reports a real 0 rather than blanking. Only the live breakdown carries
 *     this, which is why it is optional.
 *   - the connector roster, when no span count is available. An enabled-but-silent connector and a
 *     connector that was never connected are different claims, and neither is "$0.00": both blank,
 *     each saying which situation the reader is in.
 *
 * Without a span count a zero total is unresolvable by construction (a real measured zero and an
 * absent layer arrive identical), so it blanks rather than guessing.
 *
 * `unpricedCounts` (CTO-244 follow-up) is the third signal, and it overrides the second: spans we
 * observed but could not price are NOT a measured zero. A layer whose every span is unpriced has an
 * unknown cost and blanks with that reason, rather than reporting the "$0.00" a bare span count
 * would otherwise license.
 */
export function layerCoverage(
  byLayer: Readonly<Record<Layer, number>>,
  enabled: readonly Layer[],
  spanCounts?: Readonly<Partial<Record<Layer, number>>>,
  unpricedCounts?: Readonly<Partial<Record<Layer, number>>>,
): LayerCoverage[] {
  return LAYERS.map((layer) => {
    const total = byLayer[layer] ?? 0;
    const spans = spanCounts?.[layer] ?? 0;
    const unpriced = unpricedCounts?.[layer] ?? 0;
    if (spans > 0 && unpriced >= spans) {
      const label = LAYER_LABEL[layer];
      return {
        layer,
        totalMicroUsd: null,
        reason: `all ${spans.toLocaleString()} ${label} span${spans === 1 ? "" : "s"} in this window could not be priced, so the cost is unknown rather than zero`,
      };
    }
    if (total > 0) return { layer, totalMicroUsd: total, reason: "" };
    // Spans observed, at least one of them priced, and no spend: measured, and the measurement is
    // zero. (A partly unpriced layer keeps its priced figure; the table says it is a lower bound.)
    if (spans > 0) return { layer, totalMicroUsd: total, reason: "" };
    const label = LAYER_LABEL[layer];
    return {
      layer,
      totalMicroUsd: null,
      reason: enabled.includes(layer)
        ? `the ${label} connector is enabled but reported nothing in this window, so we cannot tell genuine zero spend from a connector that is not producing data`
        : `no ${label} connector is connected, so no ${label} cost was collected for this window`,
    };
  });
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
