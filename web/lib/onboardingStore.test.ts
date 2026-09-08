// SPDX-License-Identifier: Apache-2.0
import { beforeEach, describe, expect, it } from "vitest";

import {
  __resetOnboarding,
  getFunnel,
  getProgress,
  hasFunnelStage,
  recordFunnel,
} from "./onboardingStore";

describe("onboarding store", () => {
  beforeEach(() => __resetOnboarding());

  it("starts signed_up with no later milestones", () => {
    const p = getProgress();
    expect(p.signedUpAt).toBeGreaterThan(0);
    expect(p.copiedConfigAt).toBeNull();
    // #329: there is no firstTraceAt. Nothing could measure it, so the store does not carry it.
    expect(p).not.toHaveProperty("firstTraceAt");
    expect(getFunnel().map((e) => e.stage)).toEqual(["signed_up"]);
  });

  it("recordFunnel mirrors stages onto progress timestamps (first wins)", () => {
    recordFunnel("copied_config");
    const first = getProgress().copiedConfigAt;
    expect(first).not.toBeNull();
    recordFunnel("copied_config"); // second occurrence must not overwrite
    expect(getProgress().copiedConfigAt).toBe(first);
  });

  // #329: the probe-received transition reports first_trace, and it is the page NOTICING the stage
  // rather than timing it. The event is flagged so nobody reads its clock as an arrival time, and
  // it stamps no progress timestamp at all.
  it("records a noticed stage without mirroring it onto progress", () => {
    expect(hasFunnelStage("first_trace")).toBe(false);
    const ev = recordFunnel("first_trace", { noticed: true });
    expect(ev.noticed).toBe(true);
    expect(hasFunnelStage("first_trace")).toBe(true);
    expect(getProgress()).not.toHaveProperty("firstTraceAt");
    expect(getFunnel().filter((e) => e.stage === "first_trace")).toHaveLength(1);
  });

  it("marks a performed stage as measured, not noticed", () => {
    const ev = recordFunnel("copied_config");
    expect(ev.noticed).toBeUndefined();
    expect(getProgress().copiedConfigAt).toBe(ev.at);
  });
});
