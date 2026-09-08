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
  /**
   * Spans in the window we could not put a price on (CTO-244).
   *
   * `totalMicroUsd` is a sum, and ClickHouse `sum()` skips NULLs, so when this is non-zero the
   * headline is a LOWER BOUND on real spend, not the total. It is deliberately a count and not a
   * cost: the whole point is that those spans have no cost to add. The UI must disclose it rather
   * than present an under-count as complete; it must not guess what the missing spend was.
   *
   * Optional so a mock or an older payload that predates the field still typechecks and reads as
   * "nobody counted", which is what it honestly is.
   */
  unpricedSpanCount?: number;
  /** Total spans in the same window, so `unpricedSpanCount` can be stated as a share. */
  spanCount?: number;
}

export interface CostOutlier {
  runId: string;
  agent: string;
  /** CTO-244: null when any span in the run could not be priced, so the run total is unknown. */
  costMicroUsd: MicroUSD | null;
  /** CTO-244: null when the run total is unknown, or there is no priced peer median to divide by. */
  multipleOfMedian: number | null;
}

export interface FeatureRoi {
  feature: string;
  /**
   * CTO-244: null when any span for this feature could not be priced.
   *
   * This is cost divided by a user count. The numerator skips unpriced spans while the denominator
   * counts every user, so a partly-unpriced feature produced a cost-per-user that was understated
   * by an unknowable amount, and paybackDays (derived from it) then looked BETTER than reality.
   * A ratio built from mismatched populations is not a smaller number, it is a wrong one.
   */
  costPerUserMicroUsd: MicroUSD | null;
  valuePerUserMicroUsd: MicroUSD | null; // null = no value event configured
  paybackDays: number | null;
  attributionRate: number | null; // 0..1
}

export interface DataQuality {
  attributionRate: number; // 0..1
  contextDropCount: number;
  estimateCalibration: number; // fractional error, e.g. 0.021 = 2.1%
}
