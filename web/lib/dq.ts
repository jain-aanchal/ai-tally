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
    attributionRate: 0.84,
    contextDropCount24h: 3,
    estimateCalibration: 0.018,
    effectiveSampleRate: 0.22,
  },
  attribution: [
    { feature: "research_agent", rate: 0.91, events7d: 1820 },
    { feature: "support_triage", rate: 0.79, events7d: 4_320 },
    { feature: "inline_writer", rate: 0.88, events7d: 1180 },
    { feature: "chatbot", rate: 0.73, events7d: 980 },
    { feature: "smart_search", rate: 0.82, events7d: 612 },
    { feature: "summarize", rate: 0.0, events7d: 0 },
  ],
  contextDrops: [
    { service: "api-prod", sdkVersion: "py-0.0.1", drops24h: 2, spans24h: 184_000 },
    { service: "worker-prod", sdkVersion: "py-0.0.1", drops24h: 1, spans24h: 96_400 },
    { service: "edge-proxy", sdkVersion: "go-0.0.1", drops24h: 0, spans24h: 312_900 },
  ],
  calibration: [
    { date: "2026-06-06", estimatedMicroUsd: 1_420_000_000, reconciledMicroUsd: 1_402_000_000 },
    { date: "2026-06-07", estimatedMicroUsd: 1_510_000_000, reconciledMicroUsd: 1_488_000_000 },
    { date: "2026-06-08", estimatedMicroUsd: 1_580_000_000, reconciledMicroUsd: 1_561_000_000 },
    { date: "2026-06-09", estimatedMicroUsd: 1_610_000_000, reconciledMicroUsd: 1_628_000_000 },
    { date: "2026-06-10", estimatedMicroUsd: 1_660_000_000, reconciledMicroUsd: 1_641_000_000 },
    { date: "2026-06-11", estimatedMicroUsd: 1_720_000_000, reconciledMicroUsd: 1_704_000_000 },
    { date: "2026-06-12", estimatedMicroUsd: 1_750_000_000, reconciledMicroUsd: 1_734_000_000 },
  ],
  sampling: [
    { stratum: "tail", rate: 1.0, ciHalfWidthPct: 0.0, spans: 420 },
    { stratum: "mid", rate: 0.5, ciHalfWidthPct: 0.04, spans: 1840 },
    { stratum: "body", rate: 0.1, ciHalfWidthPct: 0.18, spans: 12_600 },
  ],
};
