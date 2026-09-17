// SPDX-License-Identifier: Apache-2.0
// The window the Model Comparison is actually computed over (CTO-428).
//
// The page offers a 7d / 30d / 90d / Custom selector, and the comparison used to read a fixed seven
// days regardless: the selection never reached the query, so a customer who picked 90d was shown a
// 7-day comparison with nothing saying so. A control that discards its input is worse than a wrong
// number, because it teaches the customer that their filtering works.
//
// The comparison cannot follow the selector today. The incumbent half comes out of one hardcoded
// 7-day read (`queryCurrentModel` in lib/clickhouse.ts takes no window: cost, call volume, p95 and
// error rate are all projected off that one window), and the candidate half comes from a replay
// corpus that is opted into per workload and carries no window at all. So the honest move is the
// one this module exists for: the route reports the window it really used, the workload label is
// derived from that same figure rather than from a constant that happens to match, and the page
// states the constraint at the selector so the customer sees it before choosing.

/** How many days of traffic the comparison reads. Fixed, for the reasons above. */
export const COMPARISON_WINDOW_DAYS = 7;

/** What window the comparison used, and what the caller asked for, so the two can be compared. */
export interface ComparisonWindow {
  /** Days actually read. Every figure on the page is derived from this window. */
  days: number;
  /** Days the request's range selector asked for, resolved the same way every other page does. */
  requestedDays: number;
  /** False when the comparison could not honour the requested range; the page says so. */
  honorsRequestedRange: boolean;
}

export function resolveComparisonWindow(requestedDays: number): ComparisonWindow {
  return {
    days: COMPARISON_WINDOW_DAYS,
    requestedDays,
    honorsRequestedRange: requestedDays === COMPARISON_WINDOW_DAYS,
  };
}

/**
 * The sentence shown at the range selector. It names the window the comparison really used, so it
 * cannot drift from the data the way the old fixed label could.
 */
export function comparisonWindowNotice(window: ComparisonWindow): string {
  return (
    `Comparison window: fixed at the last ${window.days} days. ` +
    "The range selector narrows the cost chart below; the candidate comparison and the tiles above " +
    "it always read this window."
  );
}
