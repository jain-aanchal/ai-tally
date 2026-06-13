// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { agents, getRun, p99Ratio, runsForAgent } from "./agents";

describe("agents data + helpers", () => {
  it("computes p99/p50 ratio", () => {
    const research = agents.find((a) => a.name === "research_agent")!;
    expect(Math.round(p99Ratio(research))).toBe(28); // 3.4M / 120k
  });

  it("flags research_agent as a tail outlier (ratio > 20)", () => {
    const research = agents.find((a) => a.name === "research_agent")!;
    expect(p99Ratio(research)).toBeGreaterThan(20);
  });

  it("inline_writer is not a tail outlier", () => {
    const writer = agents.find((a) => a.name === "inline_writer")!;
    expect(p99Ratio(writer)).toBeLessThan(20);
  });

  it("runsForAgent filters by agent", () => {
    expect(runsForAgent("research_agent").length).toBe(2);
    expect(runsForAgent("nope").length).toBe(0);
  });

  it("getRun returns a run with a why-expensive narrative + spans", () => {
    const r = getRun("research_run_8af2")!;
    expect(r).toBeDefined();
    expect(r.whyExpensive).toMatch(/retry loop/i);
    expect(r.spans.length).toBeGreaterThan(0);
    // root span has no parent
    expect(r.spans[0].parentSpanId).toBeNull();
  });
});
