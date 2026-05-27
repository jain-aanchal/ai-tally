// Mock data for the UI. Typed identically to the eventual API responses so wiring real endpoints
// later is a drop-in replacement. Clearly fake numbers.

import type { CostOutlier, DataQuality, FeatureRoi, SpendSummary } from "./types";

export const mockSpend: SpendSummary = {
  totalMicroUsd: 14_820_000_000,
  estimatedMicroUsd: 12_401_000_000,
  reconciledMicroUsd: 2_419_000_000,
  reconciledThrough: "2026-05-19",
  byLayer: {
    llm: 8_210_000_000,
    vector: 2_140_000_000,
    tools: 2_070_000_000,
    compute: 1_810_000_000,
    embeddings: 510_000_000,
    egress: 80_000_000,
  },
};

export const mockOutliers: CostOutlier[] = [
  { runId: "agent_research_run_8af2", agent: "research_agent", costMicroUsd: 3_400_000, multipleOfMedian: 78 },
  { runId: "agent_research_run_3c01", agent: "research_agent", costMicroUsd: 1_920_000, multipleOfMedian: 44 },
  { runId: "chat_completion_99fb", agent: "smart_search", costMicroUsd: 810_000, multipleOfMedian: 12 },
];

export const mockRoi: FeatureRoi[] = [
  { feature: "research_agent", costPerUserMicroUsd: 180_000, valuePerUserMicroUsd: 1_400_000, paybackDays: 13, attributionRate: 0.91 },
  { feature: "inline_writer", costPerUserMicroUsd: 40_000, valuePerUserMicroUsd: 210_000, paybackDays: 7, attributionRate: 0.88 },
  { feature: "smart_search", costPerUserMicroUsd: 20_000, valuePerUserMicroUsd: null, paybackDays: null, attributionRate: null },
];

export const mockDataQuality: DataQuality = {
  attributionRate: 0.91,
  contextDropCount: 0,
  estimateCalibration: 0.021,
};
