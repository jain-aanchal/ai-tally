// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import {
  costSeries,
  estimatedTotal,
  LAYERS,
  layerCoverage,
  reconciledTotal,
  totalForDay,
  totalRange,
  type Layer,
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

// CTO-244. LAYERS is the fixed list of layers the product knows about, not the list a tenant has
// data for, so mapping it straight onto a per-layer sum rendered "Compute $0.00, 0.0%" for a layer
// nobody ever reported. Every zero must come back as a blank carrying a real reason.
describe("layerCoverage (CTO-244)", () => {
  const zeros = (): Record<Layer, number> =>
    Object.fromEntries(LAYERS.map((l) => [l, 0])) as Record<Layer, number>;

  it("reports a measured figure for a layer carrying spend", () => {
    const byLayer = { ...zeros(), llm: 1_000_000 };
    const llm = layerCoverage(byLayer, ["llm"]).find((c) => c.layer === "llm")!;
    expect(llm.totalMicroUsd).toBe(1_000_000);
    expect(llm.reason).toBe("");
  });

  it("never fabricates a zero: a layer with no spend blanks instead", () => {
    for (const c of layerCoverage(zeros(), [])) {
      expect(c.totalMicroUsd).toBeNull();
      expect(c.reason.length).toBeGreaterThan(0);
    }
  });

  it("gives a different reason for an enabled connector than for a missing one", () => {
    const byLayer = { ...zeros(), llm: 500 };
    const rows = layerCoverage(byLayer, ["llm", "vector"]);
    const vector = rows.find((c) => c.layer === "vector")!;
    const compute = rows.find((c) => c.layer === "compute")!;
    expect(vector.totalMicroUsd).toBeNull();
    expect(compute.totalMicroUsd).toBeNull();
    // Enabled but silent: we cannot tell genuine zero spend from a connector producing nothing.
    expect(vector.reason).toContain("enabled");
    // Never connected: nothing was ever collected.
    expect(compute.reason).toContain("no Compute connector is connected");
    expect(vector.reason).not.toBe(compute.reason);
  });

  it("covers every known layer exactly once, in LAYERS order", () => {
    expect(layerCoverage(zeros(), []).map((c) => c.layer)).toEqual([...LAYERS]);
  });

  it("reports a MEASURED zero when spans were observed for the layer", () => {
    // 26k tool spans that billed nothing is a real answer, not an absence: it stays $0.00.
    const tools = layerCoverage(zeros(), [], { tools: 26_409 }).find((c) => c.layer === "tools")!;
    expect(tools.totalMicroUsd).toBe(0);
    expect(tools.reason).toBe("");
  });

  it("still blanks a zero layer whose span count is itself zero", () => {
    const compute = layerCoverage(zeros(), [], { tools: 5 }).find((c) => c.layer === "compute")!;
    expect(compute.totalMicroUsd).toBeNull();
    expect(compute.reason.length).toBeGreaterThan(0);
  });
});
