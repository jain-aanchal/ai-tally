// SPDX-License-Identifier: Apache-2.0
// CTO-429. Whether a cost figure on /attribution is a measurement or an artefact of NULL-skipping.
//
// The per-system table published `formatUSD(sum(EstimatedCost))` straight from the query. ClickHouse
// `sum()` skips NULLs, so a system whose spans ALL lack a catalog rate sums to 0 rather than to
// NULL, and `<Money>`, which blanks a null, had nothing to blank on. The page then printed "$0.00"
// for a real cost under a footnote asserting every row was real spend, on the very page that
// divides cost by conversions.
//
// The counts this reads are the ones querySpendSummary and the explore breakdown already carry for
// exactly this purpose (unpriced spans beside total spans); this is only the decision they feed.

import { isZeroRoundingLowerBound, type MicroUSD } from "@/lib/types";

/** A cost to render: `micro` null with a `reason` when it cannot honestly be shown. */
export interface CostCoverage {
  micro: MicroUSD | null;
  /** Empty when `micro` is a real figure. Written for a reader hovering the blank. */
  reason: string;
}

/**
 * Decide whether `costMicroUsd` may be shown.
 *
 * `scope` completes the sentence "spans {scope}", e.g. "for this system in this window".
 *
 * Three outcomes, in the order they are tested:
 *
 * 1. Nothing priced. The sum is 0 because there was nothing to add, so the cost is unknown.
 * 2. Partly priced, and the priced remainder rounds away at display precision (CTO-423). The
 *    reader sees a zero whatever marker sits beside it, so it blanks for the same reason as (1).
 * 3. Otherwise the figure stands, INCLUDING a genuine measured zero: spans observed, none
 *    unpriced, no spend. Hiding that would be the opposite error, a measurement withheld.
 */
export function costCoverage(
  costMicroUsd: MicroUSD,
  spanCount: number,
  unpricedSpanCount: number,
  scope: string,
): CostCoverage {
  if (spanCount > 0 && unpricedSpanCount >= spanCount) {
    const n = spanCount.toLocaleString();
    return {
      micro: null,
      reason: `none of the ${n} span${spanCount === 1 ? "" : "s"} ${scope} carry a catalog rate, so the cost is unknown rather than zero`,
    };
  }
  if (isZeroRoundingLowerBound(costMicroUsd, unpricedSpanCount)) {
    const priced = Math.max(0, spanCount - unpricedSpanCount);
    return {
      micro: null,
      reason: `only ${priced.toLocaleString()} of ${spanCount.toLocaleString()} spans ${scope} could be priced, and what we could price rounds to zero, so the cost is unknown rather than zero`,
    };
  }
  return { micro: costMicroUsd, reason: "" };
}
