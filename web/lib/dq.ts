// SPDX-License-Identifier: Apache-2.0
// Mock data for the Data Quality surface (CTO-79). Typed for the eventual API.

export type Health = "good" | "warn" | "bad";

export interface AttributionByFeature {
  feature: string;
  rate: number; // 0..1
  events7d: number;
}

export interface ContextDropsByService {
  service: string;
  sdkVersion: string;
  drops24h: number;
  /** CTO-118: total span count over the same 24h window. 0 → service inactive → render "—". */
  spans24h?: number;
}

export interface CalibrationDay {
  date: string;
  estimatedMicroUsd: number;
  reconciledMicroUsd: number;
}

export interface SampleByStratum {
  stratum: "body" | "mid" | "tail";
  rate: number; // 0..1, average configured keep rate seen in this stratum over the window
  /**
   * 0..1 fractional half-width of the 95% CI on extrapolated cost. `null` when fewer than 30
   * kept spans landed in this stratum over the window — Wilson-flavoured estimators get
   * uselessly wide below that. Render `—`, never a fabricated tight band (CTO-119).
   */
  ciHalfWidthPct: number | null;
  /** kept-span count in this stratum over the window (used to gate the CI). */
  spans: number;
}

export interface DataQualityReport {
  overall: {
    attributionRate: number;
    contextDropCount24h: number;
    estimateCalibration: number; // |est - recon| / recon, last reconciled period
    effectiveSampleRate: number; // weighted across strata
  };
  attribution: AttributionByFeature[];
  contextDrops: ContextDropsByService[];
  calibration: CalibrationDay[];
  sampling: SampleByStratum[];
}

export function classify(metric: "attribution" | "drops" | "calibration", v: number): Health {
  if (metric === "attribution") return v >= 0.9 ? "good" : v >= 0.75 ? "warn" : "bad";
  if (metric === "drops") return v === 0 ? "good" : v < 10 ? "warn" : "bad";
  // calibration: smaller is better
  return v < 0.03 ? "good" : v < 0.07 ? "warn" : "bad";
}

export const dq: DataQualityReport = {
  overall: {
    attributionRate: 0.91,
    contextDropCount24h: 0,
    estimateCalibration: 0.021,
    effectiveSampleRate: 0.22,
  },
  attribution: [
    { feature: "research_agent", rate: 0.91, events7d: 1820 },
    { feature: "inline_writer", rate: 0.88, events7d: 1180 },
    { feature: "smart_search", rate: 0.0, events7d: 0 },
  ],
  contextDrops: [
    { service: "api-prod", sdkVersion: "py-0.0.1", drops24h: 0 },
    { service: "worker-prod", sdkVersion: "py-0.0.1", drops24h: 0 },
  ],
  calibration: [
    { date: "2026-05-13", estimatedMicroUsd: 950_000_000, reconciledMicroUsd: 935_000_000 },
    { date: "2026-05-14", estimatedMicroUsd: 980_000_000, reconciledMicroUsd: 968_000_000 },
    { date: "2026-05-15", estimatedMicroUsd: 1_020_000_000, reconciledMicroUsd: 1_004_000_000 },
    { date: "2026-05-16", estimatedMicroUsd: 1_010_000_000, reconciledMicroUsd: 1_018_000_000 },
    { date: "2026-05-17", estimatedMicroUsd: 1_050_000_000, reconciledMicroUsd: 1_028_000_000 },
    { date: "2026-05-18", estimatedMicroUsd: 1_080_000_000, reconciledMicroUsd: 1_062_000_000 },
    { date: "2026-05-19", estimatedMicroUsd: 1_120_000_000, reconciledMicroUsd: 1_101_000_000 },
  ],
  sampling: [
    { stratum: "tail", rate: 1.0, ciHalfWidthPct: 0.0, spans: 420 },
    { stratum: "mid", rate: 0.5, ciHalfWidthPct: 0.04, spans: 1840 },
    { stratum: "body", rate: 0.1, ciHalfWidthPct: 0.18, spans: 12_600 },
  ],
};
