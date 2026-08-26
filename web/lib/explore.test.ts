// SPDX-License-Identifier: Apache-2.0
// Tests for the pure explore transforms (CTO-221, D1): group capping, the day pivot, window
// clamping, and the FilterState -> ExploreParams mapping. The SQL in queryCostExplore is exercised
// live against ClickHouse (numbers in the PR body); these cover the logic that shapes its output.

import { describe, expect, it } from "vitest";

import {
  MAX_WINDOW_DAYS,
  OTHER_GROUP,
  type ExploreCostRow,
  type ExploreGroupTotal,
  capGroups,
  clampWindowDays,
  exploreParamsFromFilters,
  pivotExploreDays,
} from "./explore";
import { defaultFilterState, toggleDimensionValue, withCustomRange, withGroupBy } from "./filters";

function total(group: string, totalMicroUsd: number, spanCount = 1): ExploreGroupTotal {
  return { group, totalMicroUsd, spanCount };
}

describe("capGroups", () => {
  it("keeps every group and folds nothing when under the cap", () => {
    const c = capGroups([total("a", 30), total("b", 20), total("c", 10)], 5);
    expect(c.orderedGroups).toEqual(["a", "b", "c"]);
    expect(c.truncatedGroups).toBe(0);
    expect(c.totalMicroUsd).toBe(60);
    expect(c.breakdown.some((r) => r.group === OTHER_GROUP)).toBe(false);
  });

  it("ranks by total desc, keeps the top N and folds the tail into an honest other row", () => {
    const c = capGroups([total("a", 10), total("b", 50), total("c", 30), total("d", 5, 3)], 2);
    // Kept: b (50), c (30). Folded: a (10) + d (5) = 15, spans 1 + 3 = 4.
    expect(c.orderedGroups).toEqual(["b", "c", OTHER_GROUP]);
    expect(c.truncatedGroups).toBe(2);
    const other = c.breakdown.find((r) => r.group === OTHER_GROUP)!;
    expect(other.totalMicroUsd).toBe(15);
    expect(other.spanCount).toBe(4);
    // The grand total is over ALL groups, kept and folded, so it never disagrees with the tenant.
    expect(c.totalMicroUsd).toBe(95);
    expect(c.keptGroups.has("b")).toBe(true);
    expect(c.keptGroups.has("a")).toBe(false);
  });

  it("handles an empty input", () => {
    const c = capGroups([]);
    expect(c.orderedGroups).toEqual([]);
    expect(c.totalMicroUsd).toBe(0);
    expect(c.truncatedGroups).toBe(0);
  });
});

describe("pivotExploreDays", () => {
  const dayList = ["2026-06-01", "2026-06-02", "2026-06-03"];
  const rows: ExploreCostRow[] = [
    { day: "2026-06-01", group: "a", costMicroUsd: 100 },
    { day: "2026-06-01", group: "b", costMicroUsd: 50 },
    { day: "2026-06-02", group: "a", costMicroUsd: 200 },
    { day: "2026-06-03", group: OTHER_GROUP, costMicroUsd: 10 },
  ];

  it("fills every day in the list, leaving a day with no rows as a real zero", () => {
    const days = pivotExploreDays(dayList, rows, new Set(["a", "b"]), true);
    expect(days.map((d) => d.date)).toEqual(dayList);
    expect(days[0].byGroup).toEqual({ a: 100, b: 50 });
    expect(days[1].byGroup).toEqual({ a: 200 });
    expect(days[2].byGroup).toEqual({ [OTHER_GROUP]: 10 });
  });

  it("folds a group not in keptGroups into other when hasOther, and drops it otherwise", () => {
    const folded = pivotExploreDays(
      dayList,
      [{ day: "2026-06-01", group: "z", costMicroUsd: 7 }],
      new Set(["a"]),
      true,
    );
    expect(folded[0].byGroup).toEqual({ [OTHER_GROUP]: 7 });

    const dropped = pivotExploreDays(
      dayList,
      [{ day: "2026-06-01", group: "z", costMicroUsd: 7 }],
      new Set(["a"]),
      false,
    );
    expect(dropped[0].byGroup).toEqual({});
  });

  it("ignores rows whose day is not in the authoritative list rather than shifting the axis", () => {
    const days = pivotExploreDays(
      dayList,
      [{ day: "2025-01-01", group: "a", costMicroUsd: 999 }],
      new Set(["a"]),
      false,
    );
    expect(days.every((d) => Object.keys(d.byGroup).length === 0)).toBe(true);
  });
});

describe("clampWindowDays", () => {
  it("bounds the window to [1, MAX_WINDOW_DAYS]", () => {
    expect(clampWindowDays(30)).toBe(30);
    expect(clampWindowDays(0)).toBe(1);
    expect(clampWindowDays(-5)).toBe(1);
    expect(clampWindowDays(10_000)).toBe(MAX_WINDOW_DAYS);
    expect(clampWindowDays(Number.NaN)).toBe(1);
    expect(clampWindowDays(30.9)).toBe(30);
  });
});

describe("exploreParamsFromFilters", () => {
  it("maps a preset range and excludes the group-by dimension from its own filter", () => {
    let s = withGroupBy(defaultFilterState(), "provider");
    s = toggleDimensionValue(s, "provider", "openai"); // filter on the grouped dim -> dropped
    s = toggleDimensionValue(s, "model", "gpt-4o"); // filter on another dim -> kept
    const p = exploreParamsFromFilters(s);
    expect(p.groupBy).toBe("provider");
    expect(p.window).toEqual({ kind: "preset", days: 30 });
    expect(p.filters?.provider).toBeUndefined();
    expect(p.filters?.model).toEqual(["gpt-4o"]);
  });

  it("maps a custom range to a range window", () => {
    const s = withCustomRange(defaultFilterState(), "2026-06-01", "2026-06-15");
    const p = exploreParamsFromFilters(s);
    expect(p.window).toEqual({ kind: "range", from: "2026-06-01", to: "2026-06-15" });
  });
});
