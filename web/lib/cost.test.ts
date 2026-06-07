// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import {
  costSeries,
  estimatedTotal,
  reconciledTotal,
  totalForDay,
  totalRange,
} from "./cost";

describe("cost series", () => {
  it("totalForDay sums all layers", () => {
    const t = totalForDay(costSeries.days[0]);
    expect(t).toBeGreaterThan(0);
  });

  it("totalRange = reconciled + estimated (no overlap, no gap)", () => {
    expect(reconciledTotal(costSeries) + estimatedTotal(costSeries)).toBe(totalRange(costSeries));
  });

  it("reconciled days are <= reconciledThrough boundary", () => {
    const cutoff = costSeries.reconciledThrough;
    const reconciledDays = costSeries.days.filter((d) => d.date <= cutoff);
    const estimatedDays = costSeries.days.filter((d) => d.date > cutoff);
    expect(reconciledDays.length).toBeGreaterThan(0);
    expect(estimatedDays.length).toBeGreaterThan(0);
  });

  it("vector spike emerges after the boundary (the hidden-cost story)", () => {
    const cutoff = costSeries.reconciledThrough;
    const before = costSeries.days.filter((d) => d.date <= cutoff).at(-1)!;
    const after = costSeries.days.at(-1)!;
    expect(after.byLayer.vector).toBeGreaterThan(before.byLayer.vector * 2);
  });
});
