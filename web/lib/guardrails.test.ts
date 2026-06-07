// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  GUARDRAIL_MODES,
  type GuardrailRule,
  fireRate,
  graduationSignal,
  guardrailRules,
  isActionable,
  modeMeta,
  summarize,
} from "./guardrails";

function rule(over: Partial<GuardrailRule> = {}): GuardrailRule {
  return {
    id: "gr_x",
    scopeKind: "agent",
    scope: "x",
    mode: "observe",
    maxCostMicroUsd: 1_000_000,
    maxSteps: null,
    wouldHaveFiredThisWeek: 0,
    runsThisWeek: 1_000,
    ...over,
  };
}

describe("guardrail modes", () => {
  it("modes are ordered weakest → strongest; only observe is non-enforcing", () => {
    expect(GUARDRAIL_MODES[0].mode).toBe("observe");
    expect(GUARDRAIL_MODES[0].enforcing).toBe(false);
    expect(GUARDRAIL_MODES.slice(1).every((m) => m.enforcing)).toBe(true);
  });

  it("modeMeta falls back to observe for an unknown mode", () => {
    // @ts-expect-error testing the defensive fallback
    expect(modeMeta("bogus").mode).toBe("observe");
  });
});

describe("fireRate", () => {
  it("is the fired/runs fraction, 0 when no runs", () => {
    expect(fireRate(rule({ wouldHaveFiredThisWeek: 50, runsThisWeek: 1_000 }))).toBeCloseTo(0.05);
    expect(fireRate(rule({ runsThisWeek: 0 }))).toBe(0);
  });
});

describe("graduationSignal", () => {
  it("insufficient-data below 100 runs", () => {
    expect(graduationSignal(rule({ runsThisWeek: 99, wouldHaveFiredThisWeek: 10 }))).toBe(
      "insufficient-data",
    );
  });

  it("ready when a small non-zero fraction fires", () => {
    expect(graduationSignal(rule({ runsThisWeek: 1_000, wouldHaveFiredThisWeek: 30 }))).toBe(
      "ready",
    );
  });

  it("review when nothing fires", () => {
    expect(graduationSignal(rule({ runsThisWeek: 1_000, wouldHaveFiredThisWeek: 0 }))).toBe(
      "review",
    );
  });

  it("noisy when a large fraction fires", () => {
    expect(graduationSignal(rule({ runsThisWeek: 1_000, wouldHaveFiredThisWeek: 400 }))).toBe(
      "noisy",
    );
  });
});

describe("isActionable", () => {
  it("requires a cost cap or a step cap", () => {
    expect(isActionable({ maxCostMicroUsd: null, maxSteps: null })).toBe(false);
    expect(isActionable({ maxCostMicroUsd: 1, maxSteps: null })).toBe(true);
    expect(isActionable({ maxCostMicroUsd: null, maxSteps: 5 })).toBe(true);
  });
});

describe("summarize", () => {
  it("counts enforcing, observing and graduation-ready", () => {
    const s = summarize(guardrailRules);
    expect(s.total).toBe(guardrailRules.length);
    expect(s.enforcing + s.observing).toBe(s.total);
    // research_agent observe rule fires 312/8680 ≈ 3.6% -> ready
    expect(s.readyToGraduate).toBeGreaterThanOrEqual(1);
  });
});

describe("seed rules", () => {
  it("every rule is actionable and uses a known mode", () => {
    for (const r of guardrailRules) {
      expect(isActionable(r)).toBe(true);
      expect(GUARDRAIL_MODES.some((m) => m.mode === r.mode)).toBe(true);
      expect(r.wouldHaveFiredThisWeek).toBeLessThanOrEqual(r.runsThisWeek);
    }
  });
});
