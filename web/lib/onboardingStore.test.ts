// SPDX-License-Identifier: Apache-2.0
import { beforeEach, describe, expect, it } from "vitest";

import {
  __resetOnboarding,
  getFunnel,
  getProgress,
  markFirstTrace,
  recordFunnel,
} from "./onboardingStore";

describe("onboarding store", () => {
  beforeEach(() => __resetOnboarding());

  it("starts signed_up with no later milestones", () => {
    const p = getProgress();
    expect(p.signedUpAt).toBeGreaterThan(0);
    expect(p.copiedConfigAt).toBeNull();
    expect(p.firstTraceAt).toBeNull();
    expect(getFunnel().map((e) => e.stage)).toEqual(["signed_up"]);
  });

  it("recordFunnel mirrors stages onto progress timestamps (first wins)", () => {
    recordFunnel("copied_config");
    const first = getProgress().copiedConfigAt;
    expect(first).not.toBeNull();
    recordFunnel("copied_config"); // second occurrence must not overwrite
    expect(getProgress().copiedConfigAt).toBe(first);
  });

  it("markFirstTrace is idempotent and sets firstTraceAt once", () => {
    const p1 = markFirstTrace();
    expect(p1.firstTraceAt).not.toBeNull();
    const p2 = markFirstTrace();
    expect(p2.firstTraceAt).toBe(p1.firstTraceAt);
    expect(getFunnel().filter((e) => e.stage === "first_trace")).toHaveLength(1);
  });
});
