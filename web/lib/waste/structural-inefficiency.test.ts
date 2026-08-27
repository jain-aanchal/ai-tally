// SPDX-License-Identifier: Apache-2.0
// Tests for the structural-inefficiency detector (CTO-233, W6; epic CTO-227). Each case pins one
// guarantee: a run far above its feature's median input tokens flags with a marginal (not total)
// recoverable; a run at the median does not; a runaway-step run flags on the agent scope; and a
// uniformly heavy feature (all runs high, TIGHT spread) does NOT flag every run -- the property that
// separates robust stats (median + IQR fence) from a mean+stddev fence that the outliers themselves
// would drag toward every member.

import { describe, expect, it } from "vitest";

import {
  detectStructuralInefficiency,
  type StructuralRunRow,
} from "./structural-inefficiency";

const USD = 1_000_000;

/** Build a run with sensible defaults; override only what a case cares about. */
function run(over: Partial<StructuralRunRow> = {}): StructuralRunRow {
  return {
    runId: "t0",
    agent: "research_agent",
    feature: "chatbot",
    model: "claude-sonnet-4.5",
    inputTokens: 10_000,
    outputTokens: 1_000,
    steps: 4,
    costMicroUsd: 100 * USD,
    ...over,
  };
}

/** `n` near-identical baseline runs, ids b0..b(n-1), so a cohort has a stable median. */
function baseline(n: number, over: Partial<StructuralRunRow> = {}): StructuralRunRow[] {
  return Array.from({ length: n }, (_, i) => run({ runId: `b${i}`, ...over }));
}

describe("detectStructuralInefficiency: context bloat", () => {
  it("flags a run at ~5x the feature median input tokens with a marginal recoverable", () => {
    const rows = [
      ...baseline(6, { inputTokens: 10_000, outputTokens: 1_000, costMicroUsd: 10 * USD }),
      run({
        runId: "bloated",
        inputTokens: 50_000, // 5x the 10k median
        outputTokens: 1_000,
        costMicroUsd: 50 * USD,
      }),
    ];

    const findings = detectStructuralInefficiency(rows);
    const bloat = findings.filter((f) => f.evidence.signal === "context-bloat");
    expect(bloat).toHaveLength(1);

    const f = bloat[0];
    expect(f.category).toBe("structural_inefficiency");
    expect(f.scopeKind).toBe("feature");
    expect(f.scopeValue).toBe("chatbot");
    expect(f.confidence).toBe("medium");
    expect(f.drillHref).toBe("/agents");
    expect(f.evidence.median).toBe(10_000);
    expect(f.evidence.observed).toBe(50_000);
    expect(f.evidence.exampleTrace).toBe("bloated");

    // Marginal, not total: the excess is 40k of the 51k tokens on the run, so recoverable is that
    // fraction of the $50 run cost (~$39.2), strictly less than the whole run cost.
    expect(f.recoverableMicroUsd).not.toBeNull();
    expect(f.recoverableMicroUsd!).toBeGreaterThan(0);
    expect(f.recoverableMicroUsd!).toBeLessThan(50 * USD);
    expect(f.recoverableMicroUsd!).toBe(Math.round((50 * USD * 40_000) / 51_000));
  });

  it("does not flag a cohort sitting at its own median (no deviation)", () => {
    const rows = baseline(8, { inputTokens: 12_000 });
    const findings = detectStructuralInefficiency(rows);
    expect(findings.filter((f) => f.evidence.signal === "context-bloat")).toHaveLength(0);
  });

  it("does not flag every run of a uniformly heavy feature with a tight spread", () => {
    // Every run is genuinely heavy (~100k input tokens) but the spread is tight: this is a
    // legitimately long-context feature, not waste. A mean+stddev fence would flag the top of the
    // distribution; the median + relative floor must flag NONE, because nothing reaches 3x the median.
    const rows = [
      run({ runId: "h0", inputTokens: 98_000 }),
      run({ runId: "h1", inputTokens: 99_000 }),
      run({ runId: "h2", inputTokens: 100_000 }),
      run({ runId: "h3", inputTokens: 101_000 }),
      run({ runId: "h4", inputTokens: 102_000 }),
      run({ runId: "h5", inputTokens: 103_000 }),
      run({ runId: "h6", inputTokens: 105_000 }),
    ];
    const findings = detectStructuralInefficiency(rows);
    expect(findings.filter((f) => f.evidence.signal === "context-bloat")).toHaveLength(0);
  });

  it("judges each feature against its own median, not a global one", () => {
    // A heavy feature ("summarizer", uniform ~80k) sits alongside a light feature ("chatbot", ~5k)
    // with one bloated 30k run. Only the chatbot run deviates from ITS OWN norm; the summarizer, far
    // larger in absolute terms, does not flag.
    const rows = [
      ...baseline(6, { feature: "summarizer", inputTokens: 80_000, costMicroUsd: 40 * USD }),
      ...baseline(6, { feature: "chatbot", inputTokens: 5_000, costMicroUsd: 5 * USD }),
      run({
        runId: "chatbot-bloat",
        feature: "chatbot",
        inputTokens: 30_000,
        outputTokens: 1_000,
        costMicroUsd: 30 * USD,
      }),
    ];
    const bloat = detectStructuralInefficiency(rows).filter(
      (f) => f.evidence.signal === "context-bloat",
    );
    expect(bloat).toHaveLength(1);
    expect(bloat[0].scopeValue).toBe("chatbot");
  });

  it("does not judge a cohort too small to have a norm", () => {
    // Two runs is not a norm; even a wildly larger second run yields no finding.
    const rows = [
      run({ runId: "s0", inputTokens: 5_000 }),
      run({ runId: "s1", inputTokens: 90_000 }),
    ];
    expect(detectStructuralInefficiency(rows)).toHaveLength(0);
  });
});

describe("detectStructuralInefficiency: runaway loops", () => {
  it("flags a runaway-step run on the agent scope with a marginal recoverable", () => {
    const rows = [
      ...baseline(6, { steps: 4, inputTokens: 8_000, costMicroUsd: 5 * USD }),
      run({
        runId: "loop",
        steps: 30, // far above the 4-step median
        inputTokens: 8_000,
        costMicroUsd: 40 * USD,
      }),
    ];

    const findings = detectStructuralInefficiency(rows);
    const loop = findings.filter((f) => f.evidence.signal === "runaway-loop");
    expect(loop).toHaveLength(1);

    const f = loop[0];
    expect(f.scopeKind).toBe("agent");
    expect(f.scopeValue).toBe("research_agent");
    expect(f.confidence).toBe("medium");
    expect(f.evidence.median).toBe(4);
    expect(f.evidence.observed).toBe(30);
    expect(f.evidence.exampleTrace).toBe("loop");

    // Marginal: 26 of the 30 steps are excess, so recoverable is that fraction of the $40 run cost.
    expect(f.recoverableMicroUsd).toBe(Math.round((40 * USD * 26) / 30));
    expect(f.recoverableMicroUsd!).toBeLessThan(40 * USD);
  });

  it("does not flag an agent whose run lengths are uniformly short", () => {
    const rows = baseline(8, { steps: 3 });
    expect(
      detectStructuralInefficiency(rows).filter((f) => f.evidence.signal === "runaway-loop"),
    ).toHaveLength(0);
  });
});
