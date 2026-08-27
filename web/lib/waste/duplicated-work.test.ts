// SPDX-License-Identifier: Apache-2.0
// Tests for the duplicated/retried-work detector (CTO-230, W3; epic CTO-227). Each case pins one
// rule of the shape-and-timing heuristic that would otherwise regress silently: an error-then-retry
// pair charges the failed attempt as recoverable, two well-separated runs are NOT a duplicate, a
// rapid burst of three charges two as recoverable, and distinct users never cluster together.

import { describe, expect, it } from "vitest";

import {
  detectDuplicatedWork,
  type DuplicatedWorkCluster,
  type DuplicatedWorkRun,
} from "./duplicated-work";

const USD = 1_000_000;
const T0 = 1_700_000_000; // an arbitrary fixed epoch second, so cases read deterministically

function run(over: Partial<DuplicatedWorkRun> = {}): DuplicatedWorkRun {
  return {
    traceId: "trace",
    timestampSec: T0,
    costMicroUsd: 5 * USD,
    outcome: "success",
    ...over,
  };
}

function cluster(over: Partial<DuplicatedWorkCluster> = {}): DuplicatedWorkCluster {
  return {
    feature: "chatbot",
    agent: "vercel-chatbot-demo",
    model: "gpt-4o",
    userIdHash: "u1",
    runs: [],
    ...over,
  };
}

describe("detectDuplicatedWork", () => {
  it("flags the failed attempt of an error-then-success pair as recoverable", () => {
    const findings = detectDuplicatedWork([
      cluster({
        runs: [
          run({ traceId: "a", timestampSec: T0, costMicroUsd: 4 * USD, outcome: "failed" }),
          run({ traceId: "b", timestampSec: T0 + 30, costMicroUsd: 6 * USD, outcome: "success" }),
        ],
      }),
    ]);

    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("duplicated_work");
    expect(f.confidence).toBe("medium");
    expect(f.evidence.pattern).toBe("error-then-retry");
    expect(f.evidence.supersededRuns).toBe(1);
    expect(f.evidence.exampleTrace).toBe("a");
    // Only the failed attempt's cost is recoverable; the succeeding retry is kept.
    expect(f.recoverableMicroUsd).toBe(4 * USD);
    expect(f.windowSpendMicroUsd).toBe(10 * USD);
  });

  it("does NOT flag two well-separated runs", () => {
    const findings = detectDuplicatedWork([
      cluster({
        runs: [
          run({ traceId: "a", timestampSec: T0, outcome: "failed" }),
          // 10 minutes later, past RETRY_WINDOW_SECONDS (5 min): a separate piece of work.
          run({ traceId: "b", timestampSec: T0 + 600, outcome: "success" }),
        ],
      }),
    ]);

    expect(findings).toEqual([]);
  });

  it("flags two of three rapid identical runs as recoverable", () => {
    const findings = detectDuplicatedWork([
      cluster({
        runs: [
          run({ traceId: "a", timestampSec: T0, costMicroUsd: 5 * USD }),
          run({ traceId: "b", timestampSec: T0 + 20, costMicroUsd: 5 * USD }),
          run({ traceId: "c", timestampSec: T0 + 40, costMicroUsd: 5 * USD }),
        ],
      }),
    ]);

    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.evidence.pattern).toBe("rapid-repeat");
    expect(f.evidence.supersededRuns).toBe(2);
    // Keep one of three identical runs; the other two (2 x 5 USD) are recoverable.
    expect(f.recoverableMicroUsd).toBe(10 * USD);
    expect(f.windowSpendMicroUsd).toBe(15 * USD);
    expect(f.evidence.windowSeconds).toBe(40);
  });

  it("does not cluster distinct users together", () => {
    // Same feature/agent/model, but two different users, each with a single run. Neither user has a
    // duplicate on their own, so nothing is flagged: the burst must form WITHIN one user's cluster.
    const findings = detectDuplicatedWork([
      cluster({ userIdHash: "u1", runs: [run({ traceId: "a", timestampSec: T0 })] }),
      cluster({ userIdHash: "u2", runs: [run({ traceId: "b", timestampSec: T0 + 10 })] }),
    ]);

    expect(findings).toEqual([]);
  });
});
