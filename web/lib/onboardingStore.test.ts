// SPDX-License-Identifier: Apache-2.0
import { beforeEach, describe, expect, it } from "vitest";

import {
  __resetOnboarding,
  getFunnel,
  getProgress,
  hasFunnelStage,
  recordFunnel,
} from "./onboardingStore";

// Two tenant UUIDs, because the canonical identifier is the tenant UUID (CLAUDE.md) and the bug
// this file now guards against was one record serving both of them.
const TENANT_A = "11111111-1111-1111-1111-111111111111";
const TENANT_B = "22222222-2222-2222-2222-222222222222";

describe("onboarding store", () => {
  beforeEach(() => __resetOnboarding());

  it("starts with no milestones and no invented timestamps", () => {
    const p = getProgress(TENANT_A);
    expect(p.copiedConfigAt).toBeNull();
    expect(p.firstDashboardAt).toBeNull();
    // #329: there is no firstTraceAt. Nothing could measure it, so the store does not carry it.
    expect(p).not.toHaveProperty("firstTraceAt");
    // #358: and no signedUpAt. It used to be stamped when the record was built, which was server
    // boot time, presented as the tenant's signup.
    expect(p).not.toHaveProperty("signedUpAt");
    // The funnel starts empty rather than seeded with a signed_up event stamped Date.now().
    expect(getFunnel(TENANT_A)).toEqual([]);
  });

  it("recordFunnel mirrors stages onto progress timestamps (first wins)", () => {
    recordFunnel(TENANT_A, "copied_config");
    const first = getProgress(TENANT_A).copiedConfigAt;
    expect(first).not.toBeNull();
    recordFunnel(TENANT_A, "copied_config"); // second occurrence must not overwrite
    expect(getProgress(TENANT_A).copiedConfigAt).toBe(first);
  });

  // #329: the probe-received transition reports first_trace, and it is the page NOTICING the stage
  // rather than timing it. The event is flagged so nobody reads its clock as an arrival time, and
  // it stamps no progress timestamp at all.
  it("records a noticed stage without mirroring it onto progress", () => {
    expect(hasFunnelStage(TENANT_A, "first_trace")).toBe(false);
    const ev = recordFunnel(TENANT_A, "first_trace", { noticed: true });
    expect(ev.noticed).toBe(true);
    expect(hasFunnelStage(TENANT_A, "first_trace")).toBe(true);
    expect(getProgress(TENANT_A)).not.toHaveProperty("firstTraceAt");
    expect(getFunnel(TENANT_A).filter((e) => e.stage === "first_trace")).toHaveLength(1);
  });

  it("marks a performed stage as measured, not noticed", () => {
    const ev = recordFunnel(TENANT_A, "copied_config");
    expect(ev.noticed).toBeUndefined();
    expect(getProgress(TENANT_A).copiedConfigAt).toBe(ev.at);
  });

  // #358, the point of the change: the store was one `globalThis.__tallyOnboarding` record shared
  // by every organization on the deployment, so one tenant's progress was visible to and
  // overwritten by another's.
  it("keeps one tenant's progress invisible to another", () => {
    recordFunnel(TENANT_A, "copied_config");
    recordFunnel(TENANT_A, "first_dashboard");

    expect(getProgress(TENANT_B).copiedConfigAt).toBeNull();
    expect(getProgress(TENANT_B).firstDashboardAt).toBeNull();
    expect(getFunnel(TENANT_B)).toEqual([]);
    expect(hasFunnelStage(TENANT_B, "copied_config")).toBe(false);

    // And B's own progress does not disturb A's.
    recordFunnel(TENANT_B, "copied_config");
    expect(getProgress(TENANT_A).firstDashboardAt).not.toBeNull();
    expect(getFunnel(TENANT_A).map((e) => e.stage)).toEqual(["copied_config", "first_dashboard"]);
    expect(getFunnel(TENANT_B).map((e) => e.stage)).toEqual(["copied_config"]);
  });
});
