// SPDX-License-Identifier: Apache-2.0
// Mock data for the Features / Business attribution workflow (CTO-70). Typed for the eventual API.

import type { MicroUSD } from "./types";

export interface FeatureEconomics {
  feature: string;
  /** value event the ROI is attributed against; null = unattributed */
  valueEvent: string | null;
  costPerUserMicroUsd: MicroUSD;
  valuePerUserMicroUsd: MicroUSD | null;
  paybackDays: number | null;
  attributionRate: number | null; // 0..1
  /** events attributed by stitching source */
  attributionBreakdown: {
    direct: number;
    sessionStitched: number;
    identityGraphStitched: number;
    unmatched: number;
  };
}

export function margin(e: FeatureEconomics): number | null {
  if (e.valuePerUserMicroUsd === null) return null;
  if (e.valuePerUserMicroUsd === 0) return 0;
  return (e.valuePerUserMicroUsd - e.costPerUserMicroUsd) / e.valuePerUserMicroUsd;
}

export interface AttributionDiagnostics {
  lateArrivalEvents7d: number;
  lateArrivalMedianHours: number;
  reconcilerLastRunMinutesAgo: number;
}

export const features: FeatureEconomics[] = [
  {
    feature: "research_agent",
    valueEvent: "subscription_created",
    costPerUserMicroUsd: 180_000,
    valuePerUserMicroUsd: 1_400_000,
    paybackDays: 13,
    attributionRate: 0.91,
    attributionBreakdown: { direct: 1313, sessionStitched: 255, identityGraphStitched: 90, unmatched: 162 },
  },
  {
    feature: "inline_writer",
    valueEvent: "paid_conversion",
    costPerUserMicroUsd: 40_000,
    valuePerUserMicroUsd: 210_000,
    paybackDays: 7,
    attributionRate: 0.88,
    attributionBreakdown: { direct: 880, sessionStitched: 120, identityGraphStitched: 35, unmatched: 145 },
  },
  {
    feature: "smart_search",
    valueEvent: null,
    costPerUserMicroUsd: 20_000,
    valuePerUserMicroUsd: null,
    paybackDays: null,
    attributionRate: null,
    attributionBreakdown: { direct: 0, sessionStitched: 0, identityGraphStitched: 0, unmatched: 0 },
  },
];

export const diagnostics: AttributionDiagnostics = {
  lateArrivalEvents7d: 180,
  lateArrivalMedianHours: 4.2,
  reconcilerLastRunMinutesAgo: 47,
};
