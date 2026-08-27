// SPDX-License-Identifier: Apache-2.0
// The flexible cost-explore contract (CTO-221, D1). Types and PURE transforms shared by the
// ClickHouse query (`queryCostExplore` in lib/clickhouse.ts) and the /api/explore route.
//
// The query itself is a thin SQL shell around these helpers: ClickHouse returns raw (day, group,
// cost) rows, and everything else — capping the group set so a high-cardinality dimension cannot
// explode the payload, folding the long tail into a single honest "other" bucket, and pivoting the
// flat rows into one point per calendar day — happens here, where it is unit-testable without a
// database.
//
// The day list is NEVER built in this file. It is threaded in from the caller, which derives it from
// the window ClickHouse reported (see queryCostExplore). This is the CTO-203 seam: a JS-built day
// list drifts a day against the SQL window across a midnight boundary and silently drops the oldest
// row while it still counts toward the totals. So `pivotExploreDays` takes the authoritative day
// list as an argument and fills it; it does not generate one.

import type { MicroUSD } from "./types";
import { DIMENSIONS, type Dimension, type FilterState, rangeDays } from "./filters";

/** The dimensions the explorer can group by. Same set as the filter dimensions (CTO-221). */
export const EXPLORE_DIMENSIONS = DIMENSIONS;
export type ExploreDimension = Dimension;

/**
 * Above this many distinct groups we stop returning individual series and fold the tail into
 * {@link OTHER_GROUP}. A stacked chart past a dozen bands is unreadable, and an unbounded group set
 * on a high-cardinality dimension (account, model) is also an unbounded payload. The kept groups are
 * the top spenders; the fold is honest because "other" carries the exact remaining total.
 */
export const MAX_EXPLORE_GROUPS = 12;

/** The synthetic bucket the long tail folds into. Not a real dimension value, so it is namespaced. */
export const OTHER_GROUP = "· other";

/** Hard ceiling on the window a single explore scan may cover, so a hand-crafted range can't ask
 *  ClickHouse for years of spans. A year is plenty for the dashboard's longest view. */
export const MAX_WINDOW_DAYS = 366;

export type ExploreWindow =
  | { kind: "preset"; days: number }
  | { kind: "range"; from: string; to: string };

export type ExploreFilters = Partial<Record<ExploreDimension, string[]>>;

export interface ExploreParams {
  window: ExploreWindow;
  groupBy: ExploreDimension;
  filters?: ExploreFilters;
}

export interface ExploreDayPoint {
  /** ISO yyyy-mm-dd, from the caller's ClickHouse-derived day list. */
  date: string;
  /** Cost per kept group for this day, in integer micro-USD. Missing group = genuinely zero. */
  byGroup: Record<string, MicroUSD>;
}

export interface ExploreBreakdownRow {
  group: string;
  totalMicroUsd: MicroUSD;
  spanCount: number;
}

export interface ExploreSeries {
  groupBy: ExploreDimension;
  /** Oldest / newest day in the window, per ClickHouse. */
  windowStart: string;
  windowEnd: string;
  windowDays: number;
  /** Kept group names, ordered by total spend desc, possibly ending in {@link OTHER_GROUP}. */
  groups: string[];
  /** One point per calendar day in the window, oldest to newest. */
  days: ExploreDayPoint[];
  /** The breakdown table: one row per kept group (plus "other"), ordered by total desc. */
  breakdown: ExploreBreakdownRow[];
  /** Sum over every group, so the headline can never disagree with the rows beneath it. */
  totalMicroUsd: MicroUSD;
  /** How many real groups were folded into "other". 0 when the dimension fit under the cap. */
  truncatedGroups: number;
}

/**
 * Filter-aware headline totals for the tiles (CTO-240). The Cost tiles must move with the FULL
 * filter set, not just the time window, so they read this slice total rather than the /api/cost
 * per-layer series (which only knew the window and the legacy ?tag=). The estimated / reconciled
 * split comes from CostSource, matching the Home summary, so the same slice reads the same way on
 * both pages. `reconciledThrough` is the newest invoiced day or '1970-01-01' when nothing has
 * settled (honest-under-uncertainty: a real boundary, never a fabricated recent date).
 */
export interface CostSliceTotals {
  totalMicroUsd: MicroUSD;
  estimatedMicroUsd: MicroUSD;
  reconciledMicroUsd: MicroUSD;
  reconciledThrough: string;
}

/** A raw per-group total row as it comes back from ClickHouse before capping. */
export interface ExploreGroupTotal {
  group: string;
  totalMicroUsd: MicroUSD;
  spanCount: number;
}

/** A raw per-day-per-group cost row from ClickHouse. */
export interface ExploreCostRow {
  day: string;
  group: string;
  costMicroUsd: MicroUSD;
}

export interface CappedGroups {
  /** Kept breakdown rows, top spenders first, with an appended "other" row when anything folded. */
  breakdown: ExploreBreakdownRow[];
  /** Just the kept real group names (no "other"), for fast membership tests during the pivot. */
  keptGroups: Set<string>;
  /** Ordered group names for the chart legend, "other" last when present. */
  orderedGroups: string[];
  truncatedGroups: number;
  totalMicroUsd: MicroUSD;
}

/**
 * Rank groups by total spend, keep the top {@link MAX_EXPLORE_GROUPS}, and fold the rest into one
 * "other" row whose total is the exact sum of everything dropped. The returned total is over ALL
 * groups, kept and folded alike, so it always equals the tenant's spend for the slice.
 */
export function capGroups(
  totals: readonly ExploreGroupTotal[],
  max: number = MAX_EXPLORE_GROUPS,
): CappedGroups {
  const sorted = [...totals].sort((a, b) => b.totalMicroUsd - a.totalMicroUsd);
  const grandTotal = sorted.reduce((s, r) => s + r.totalMicroUsd, 0);

  const kept = sorted.slice(0, Math.max(0, max));
  const tail = sorted.slice(Math.max(0, max));

  const breakdown: ExploreBreakdownRow[] = kept.map((r) => ({
    group: r.group,
    totalMicroUsd: r.totalMicroUsd,
    spanCount: r.spanCount,
  }));

  if (tail.length > 0) {
    breakdown.push({
      group: OTHER_GROUP,
      totalMicroUsd: tail.reduce((s, r) => s + r.totalMicroUsd, 0),
      spanCount: tail.reduce((s, r) => s + r.spanCount, 0),
    });
  }

  return {
    breakdown,
    keptGroups: new Set(kept.map((r) => r.group)),
    orderedGroups: breakdown.map((r) => r.group),
    truncatedGroups: tail.length,
    totalMicroUsd: grandTotal,
  };
}

/**
 * Pivot flat (day, group, cost) rows onto the authoritative `dayList`, folding any group not in
 * `keptGroups` into the "other" bucket so a day's bars still sum to that day's true spend. Every day
 * in `dayList` gets a point even with no rows (a real zero), and rows whose day is not in the list
 * are ignored rather than silently shifting the axis.
 */
export function pivotExploreDays(
  dayList: readonly string[],
  rows: readonly ExploreCostRow[],
  keptGroups: ReadonlySet<string>,
  hasOther: boolean,
): ExploreDayPoint[] {
  const byDay = new Map<string, ExploreDayPoint>();
  for (const date of dayList) byDay.set(date, { date, byGroup: {} });

  for (const r of rows) {
    const point = byDay.get(r.day);
    if (!point) continue;
    const key = keptGroups.has(r.group) ? r.group : hasOther ? OTHER_GROUP : null;
    if (key === null) continue;
    point.byGroup[key] = (point.byGroup[key] ?? 0) + r.costMicroUsd;
  }

  return dayList.map((date) => byDay.get(date)!);
}

/** Clamp a requested window to `[1, MAX_WINDOW_DAYS]` so a scan can't be asked to cover forever. */
export function clampWindowDays(days: number): number {
  if (!Number.isFinite(days)) return 1;
  return Math.min(MAX_WINDOW_DAYS, Math.max(1, Math.trunc(days)));
}

/**
 * Translate the URL-driven {@link FilterState} into {@link ExploreParams}. The group-by dimension is
 * excluded from its own dimension filter: grouping by provider AND filtering to one provider would
 * collapse the chart to a single band, which is never what a group-by intends. Every other
 * dimension's filter is carried through.
 */
export function exploreParamsFromFilters(state: FilterState): ExploreParams {
  const filters: ExploreFilters = {};
  for (const dim of DIMENSIONS) {
    if (dim === state.groupBy) continue;
    if (state.filters[dim].length > 0) filters[dim] = state.filters[dim];
  }

  const window: ExploreWindow =
    state.range.preset === "custom" && state.range.from && state.range.to
      ? { kind: "range", from: state.range.from, to: state.range.to }
      : { kind: "preset", days: rangeDays(state.range) };

  return { window, groupBy: state.groupBy, filters };
}
