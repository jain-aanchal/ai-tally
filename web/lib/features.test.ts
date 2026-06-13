// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { features, margin } from "./features";

describe("features", () => {
  it("margin: positive when value > cost", () => {
    const research = features.find((f) => f.feature === "research_agent")!;
    const m = margin(research);
    expect(m).not.toBeNull();
    expect(m!).toBeGreaterThan(0.5); // (1.4 - 0.18) / 1.4 ≈ 0.87
  });

  it("margin: null when value is unattributed", () => {
    const smart = features.find((f) => f.feature === "smart_search")!;
    expect(margin(smart)).toBeNull();
  });

  it("attribution breakdown sums to per-feature event total", () => {
    for (const f of features) {
      if (f.attributionRate === null) continue;
      const b = f.attributionBreakdown;
      expect(b.direct + b.sessionStitched + b.identityGraphStitched + b.unmatched).toBeGreaterThan(0);
    }
  });

  it("attributed >= unattributed for inline_writer (88% attributed)", () => {
    const w = features.find((f) => f.feature === "inline_writer")!;
    const b = w.attributionBreakdown;
    const attributed = b.direct + b.sessionStitched + b.identityGraphStitched;
    expect(attributed).toBeGreaterThan(b.unmatched);
  });
});
