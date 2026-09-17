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
import { formatUSD } from "./types";

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

  // CTO-244 follow-up: spans we could not price are not a measured zero. A span count alone used to
  // license a confident "$0.00" for a layer whose every span was unpriced.
  it("blanks a layer whose observed spans were ALL unpriced", () => {
    const tools = layerCoverage(zeros(), [], { tools: 8 }, { tools: 8 }).find(
      (c) => c.layer === "tools",
    )!;
    expect(tools.totalMicroUsd).toBeNull();
    expect(tools.reason).toContain("could not be priced");
  });

  it("keeps the priced figure of a partly unpriced layer", () => {
    const byLayer = { ...zeros(), llm: 900_000 };
    const llm = layerCoverage(byLayer, ["llm"], { llm: 10 }, { llm: 3 }).find(
      (c) => c.layer === "llm",
    )!;
    expect(llm.totalMicroUsd).toBe(900_000);
    expect(llm.reason).toBe("");
  });
});

// CTO-423. "All unpriced" was too narrow a test for "we cannot report this". A layer of 531 spans
// with 530 of them unpriced kept the priced remainder and rendered "$0.0000 at least, 100.0%": a
// figure that rounds away at display precision is read as "we measured your spend and it was
// nothing", which is a fabricated zero however it is marked.
describe("layerCoverage: a lower bound that rounds to zero (CTO-423)", () => {
  const zeros = (): Record<Layer, number> =>
    Object.fromEntries(LAYERS.map((l) => [l, 0])) as Record<Layer, number>;

  it("blanks a layer whose priced remainder formats to $0.0000", () => {
    const byLayer = { ...zeros(), llm: 5 }; // one priced span worth 5 micro-USD
    const llm = layerCoverage(byLayer, ["llm"], { llm: 531 }, { llm: 530 }).find(
      (c) => c.layer === "llm",
    )!;
    expect(formatUSD(5)).toBe("$0.0000"); // the display precision that makes this necessary
    expect(llm.totalMicroUsd).toBeNull();
    expect(llm.reason).toContain("1 of 531");
    expect(llm.reason).toContain("rounds to zero");
  });

  it("keeps a small lower bound that still renders as a figure", () => {
    // 100 micro-USD prints "$0.0001". It is a real, readable number, so the "at least" marker can
    // carry the rest of the story and the figure stays.
    const byLayer = { ...zeros(), llm: 100 };
    const llm = layerCoverage(byLayer, ["llm"], { llm: 531 }, { llm: 530 }).find(
      (c) => c.layer === "llm",
    )!;
    expect(llm.totalMicroUsd).toBe(100);
    expect(llm.reason).toBe("");
  });

  it("still reports a genuine measured zero: spans observed, none unpriced", () => {
    // The opposite error. Nothing was unpriced, so zero IS the measurement and blanking it would
    // hide a real answer.
    const tools = layerCoverage(zeros(), [], { tools: 531 }, { tools: 0 }).find(
      (c) => c.layer === "tools",
    )!;
    expect(tools.totalMicroUsd).toBe(0);
    expect(tools.reason).toBe("");
  });
});
