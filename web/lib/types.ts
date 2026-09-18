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

/**
 * Does `micro` vanish at the precision `formatUSD` will print it at? (CTO-423)
 *
 * Asked by running the formatter rather than by comparing against a threshold of its own: the
 * display precision is scaled per value inside formatUSD, and a second copy of that scale here
 * would drift the first time either side changed. If the formatter prints no non-zero digit, the
 * reader sees a zero, whatever the underlying integer was.
 */
export function roundsToZeroUSD(micro: MicroUSD): boolean {
  return !/[1-9]/.test(formatUSD(micro));
}

/**
 * Is this figure a lower bound that the reader would read as a measured zero? (CTO-423)
 *
 * A priced subtotal standing in for a partly unpriced population is a lower bound, and the surfaces
 * disclose that with an "at least" marker. The marker cannot carry the distinction on its own once
 * the figure rounds away: 530 unpriced spans beside one priced span worth 5 micro-USD rendered
 * "$0.0000 at least, 100.0%", which reads as "we measured your spend and it was nothing" when the
 * truth is "we could price 1 of your 531 calls". That is a fabricated zero at the render layer, so
 * the figure blanks with a reason instead, exactly as an all-unpriced population already does.
 *
 * Note this is NOT a proportional rule: a lower bound that still prints a non-zero figure keeps it,
 * however much of the population was unpriced.
 */
export function isZeroRoundingLowerBound(
  micro: MicroUSD | null,
  unpricedCount: number,
): boolean {
  return micro !== null && unpricedCount > 0 && roundsToZeroUSD(micro);
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
  reconciledThrough: string; // ISO date, boundary between reconciled and estimated
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
  /**
   * Spans observed per layer in the window (CTO-431).
   *
   * `byLayer` above is a sum and cannot answer "did this connector deliver anything", because a
   * layer whose spans were all unpriced sums to 0 exactly like a layer that sent nothing. Only a
   * count separates them, and the partial-data banner's claim is about delivery, not spend.
   *
   * Optional for the same reason as the two counts above: an older payload or a mock reads as
   * "nobody counted", and `zeroEnabledLayers` then declines to name a layer rather than guessing.
   */
  spansByLayer?: SpendByLayer;
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
  /**
   * 0..1, or null when there is nothing to rate (#364).
   *
   * The live read used to answer "no business events at all" with a vacuous 1.0, i.e. a confident
   * 100% attribution for a tenant that has attributed nothing. A rate over an empty population is
   * not a rate.
   */
  attributionRate: number | null; // 0..1
  /** null when no source measures this. It was a hardcoded 0, which claimed zero drops. */
  contextDropCount: number | null;
  /** Fractional error, e.g. 0.021 = 2.1%. null when nothing has reconciled to calibrate against. */
  estimateCalibration: number | null;
}
