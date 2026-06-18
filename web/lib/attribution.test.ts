// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  buildProviderRow,
  emptyReport,
  mockReport,
  parseFilters,
  wilsonInterval,
} from "./attribution";

describe("wilsonInterval", () => {
  it("returns zero band when there are no trials", () => {
    expect(wilsonInterval(0, 0)).toEqual({ p: 0, lo: 0, hi: 0 });
  });

  it("centers the band on the observed rate", () => {
    const r = wilsonInterval(5, 10);
    expect(r.p).toBeCloseTo(0.5);
    expect(r.lo).toBeLessThan(0.5);
    expect(r.hi).toBeGreaterThan(0.5);
    // Symmetric for p=0.5
    expect(r.hi - 0.5).toBeCloseTo(0.5 - r.lo, 5);
  });

  it("clamps the band to [0, 1]", () => {
    const all = wilsonInterval(10, 10);
    expect(all.hi).toBeLessThanOrEqual(1);
    expect(all.lo).toBeGreaterThanOrEqual(0);
    const none = wilsonInterval(0, 10);
    expect(none.lo).toBeGreaterThanOrEqual(0);
    expect(none.hi).toBeLessThanOrEqual(1);
  });

  it("widens the band as the sample shrinks", () => {
    const big = wilsonInterval(50, 100);
    const small = wilsonInterval(5, 10);
    expect(big.hi - big.lo).toBeLessThan(small.hi - small.lo);
  });
});

describe("buildProviderRow", () => {
  it("computes $/conversion as integer micro-USD", () => {
    const r = buildProviderRow("openai", 25, 5, 850_000);
    expect(r.costPerConversionMicroUsd).toBe(170_000);
    expect(r.conversionRate).toBeCloseTo(0.2);
  });

  it("returns null $/conversion when conversions are zero", () => {
    const r = buildProviderRow("openai", 25, 0, 500_000);
    expect(r.costPerConversionMicroUsd).toBeNull();
  });

  it("leaves value/margin null when no Stripe data is provided (CTO-110)", () => {
    const r = buildProviderRow("openai", 25, 5, 850_000);
    expect(r.valuePerUserMicroUsd).toBeNull();
    expect(r.marginPerUserMicroUsd).toBeNull();
    expect(r.marginPct).toBeNull();
  });

  it("computes value/user and positive margin/user from revenue (CTO-110)", () => {
    const r = buildProviderRow("openai", 25, 5, 1_000_000, {
      revenueMicroUsd: 10_000_000,
      distinctUsers: 10,
    });
    // 10_000_000 micro / 10 users = 1_000_000 micro per user
    expect(r.valuePerUserMicroUsd).toBe(1_000_000);
    // cost/user = 1_000_000 / 10 = 100_000 → margin = 1_000_000 − 100_000 = 900_000
    expect(r.marginPerUserMicroUsd).toBe(900_000);
    expect(r.marginPct).toBeCloseTo(0.9);
  });

  it("surfaces negative margin/user when cost outweighs value (CTO-110)", () => {
    const r = buildProviderRow("openai", 25, 5, 2_000_000, {
      revenueMicroUsd: 1_000_000,
      distinctUsers: 10,
    });
    // value/user = 100_000, cost/user = 200_000 → margin = −100_000
    expect(r.marginPerUserMicroUsd).toBe(-100_000);
    expect(r.marginPct).toBeCloseTo(-1);
  });

  it("treats zero distinct users as no Stripe data (no fabrication)", () => {
    const r = buildProviderRow("openai", 25, 5, 1_000_000, {
      revenueMicroUsd: 50_000,
      distinctUsers: 0,
    });
    expect(r.valuePerUserMicroUsd).toBeNull();
    expect(r.marginPerUserMicroUsd).toBeNull();
  });
});

describe("parseFilters", () => {
  it("reads tag/provider/outcome from URLSearchParams", () => {
    const sp = new URLSearchParams("tag=chatbot-demo&provider=anthropic&outcome=positive_feedback");
    expect(parseFilters(sp)).toEqual({
      tag: "chatbot-demo",
      provider: "anthropic",
      outcome: "positive_feedback",
    });
  });

  it("rejects unknown provider/outcome values silently (filter dropped)", () => {
    const sp = new URLSearchParams("provider=mystery&outcome=mystery");
    expect(parseFilters(sp)).toEqual({ tag: null, provider: null, outcome: null });
  });

  it("treats an empty tag as missing", () => {
    expect(parseFilters(new URLSearchParams("tag=")).tag).toBeNull();
  });
});

describe("emptyReport / mockReport", () => {
  it("emptyReport is honest — no providers, no costs", () => {
    const r = emptyReport({ tag: null, provider: null, outcome: null });
    expect(r.perProvider).toHaveLength(0);
    expect(r.totals.sessions).toBe(0);
    expect(r.totals.costPerConversionMicroUsd).toBeNull();
    expect(r.isMock).toBe(false);
  });

  it("mockReport has both providers and is flagged as mock", () => {
    const r = mockReport({ tag: "chatbot-demo", provider: null, outcome: "conversion" });
    expect(r.isMock).toBe(true);
    expect(r.perProvider.map((p) => p.provider).sort()).toEqual([
      "anthropic",
      "openai",
    ]);
    expect(r.totals.costPerConversionMicroUsd).not.toBeNull();
  });
});
