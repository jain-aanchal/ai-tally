// SPDX-License-Identifier: Apache-2.0
// Shared UI types. These mirror the SDK/wire shapes (micro-USD integers for money) so swapping
// mock data for real API responses later is a drop-in.

export type MicroUSD = number; // integer micro-dollars (1e-6 USD)

export function formatUSD(micro: MicroUSD): string {
  const usd = micro / 1_000_000;
  // Per-call AI costs are routinely sub-cent. The default 2-decimal currency
  // format would floor $0.0032 to "$0.00" and erase the signal. Scale precision
  // to the value: small numbers get up to 4 decimals, large ones stay at 2.
  const abs = Math.abs(usd);
  let fractionDigits: number;
  if (abs === 0) fractionDigits = 2;
  else if (abs < 0.01) fractionDigits = 4;
  else if (abs < 1) fractionDigits = 3;
  else fractionDigits = 2;
  return usd.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

export interface SpendByLayer {
  llm: MicroUSD;
  vector: MicroUSD;
  tools: MicroUSD;
  compute: MicroUSD;
  embeddings: MicroUSD;
  egress: MicroUSD;
}

export interface SpendSummary {
  totalMicroUsd: MicroUSD;
  estimatedMicroUsd: MicroUSD;
  reconciledMicroUsd: MicroUSD;
  reconciledThrough: string; // ISO date — boundary between reconciled and estimated
  byLayer: SpendByLayer;
}

export interface CostOutlier {
  runId: string;
  agent: string;
  costMicroUsd: MicroUSD;
  multipleOfMedian: number;
}

export interface FeatureRoi {
  feature: string;
  costPerUserMicroUsd: MicroUSD;
  valuePerUserMicroUsd: MicroUSD | null; // null = no value event configured
  paybackDays: number | null;
  attributionRate: number | null; // 0..1
}

export interface DataQuality {
  attributionRate: number; // 0..1
  contextDropCount: number;
  estimateCalibration: number; // fractional error, e.g. 0.021 = 2.1%
}
