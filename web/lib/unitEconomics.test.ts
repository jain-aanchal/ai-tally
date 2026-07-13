// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import type { CacPeriod } from "./cac";
import {
  DEFAULT_THRESHOLDS,
  blendedCac,
  costPerUser,
  fullyLoadedCac,
  ltv,
  ltvCacBand,
  ltvOverCac,
  marginPct,
  marginPerUser,
  marketingCac,
  paybackBand,
  paybackMonths,
  resolveThresholds,
  valuePerUser,
} from "./unitEconomics";

function p(overrides: Partial<CacPeriod> = {}): CacPeriod {
  return {
    periodStart: "2026-01-01",
    periodEnd: "2026-01-31",
    currency: "USD",
    paidSpendMicroUsd: 10_000_000,    // $10
    salesSpendMicroUsd: 20_000_000,   // $20
    contentSpendMicroUsd: 5_000_000,  // $5

    overheadMicroUsd: 15_000_000,     // $15
    newCustomersPaid: 5,
    newCustomersTotal: 10,
    notes: null,
    closedAt: null,
    locked: false,
    ...overrides,
  } as CacPeriod;
}

describe("CAC flavors", () => {
  it("marketingCac is paid / paid_customers", () => {
    expect(marketingCac(p())).toBe(10_000_000 / 5);
  });

  it("blendedCac is (paid+sales+content) / total_customers", () => {
    expect(blendedCac(p())).toBe((10_000_000 + 20_000_000 + 5_000_000) / 10);
  });

  it("fullyLoadedCac adds overhead", () => {
    expect(fullyLoadedCac(p())).toBe(
      (10_000_000 + 20_000_000 + 5_000_000 + 15_000_000) / 10,
    );
  });

  it("marketingCac is null when no paid customers (would divide by zero)", () => {
    expect(marketingCac(p({ newCustomersPaid: 0 }))).toBeNull();
  });

  it("blendedCac is null when no total customers", () => {
    expect(blendedCac(p({ newCustomersTotal: 0 }))).toBeNull();
  });
});

describe("per-user economics", () => {
  it("costPerUser divides total cost by total customers", () => {
    expect(costPerUser(p(), 100_000_000)).toBe(10_000_000);
  });

  it("valuePerUser divides revenue by total customers", () => {
    expect(valuePerUser(p(), 500_000_000)).toBe(50_000_000);
  });

  it("marginPerUser allows NEGATIVE — don't clamp", () => {
    // Honest: business loses money per user when cost > value
    expect(marginPerUser(10, 30)).toBe(-20);
  });

  it("marginPerUser is null if either input is null", () => {
    expect(marginPerUser(null, 10)).toBeNull();
    expect(marginPerUser(10, null)).toBeNull();
  });

  it("marginPct is null when value is 0 (no denominator)", () => {
    expect(marginPct(0, 10)).toBeNull();
  });

  it("marginPct can be negative", () => {
    expect(marginPct(100, 130)).toBe((100 - 130) / 100);
  });
});

describe("payback months", () => {
  it("is cac / margin when margin > 0", () => {
    expect(paybackMonths(120, 10)).toBe(12);
  });

  it("is NULL when margin is zero — not Infinity", () => {
    // Honest: zero margin means we never recoup CAC.
    expect(paybackMonths(120, 0)).toBeNull();
  });

  it("is NULL when margin is NEGATIVE — business losing money", () => {
    expect(paybackMonths(120, -5)).toBeNull();
  });

  it("propagates nulls", () => {
    expect(paybackMonths(null, 10)).toBeNull();
    expect(paybackMonths(120, null)).toBeNull();
  });
});

describe("LTV / CAC", () => {
  it("ltv is margin * retentionMonths", () => {
    expect(ltv(10, 24)).toBe(240);
  });

  it("ltv preserves negative sign (honest)", () => {
    expect(ltv(-5, 24)).toBe(-120);
  });

  it("ltvOverCac is ltv / cac", () => {
    expect(ltvOverCac(240, 80)).toBe(3);
  });

  it("ltvOverCac is null when cac is 0", () => {
    expect(ltvOverCac(240, 0)).toBeNull();
  });

  it("band thresholds: >3 green, [1,3] yellow, <1 red", () => {
    expect(ltvCacBand(3.5)).toBe("green");
    expect(ltvCacBand(3.0)).toBe("yellow");
    expect(ltvCacBand(2.0)).toBe("yellow");
    expect(ltvCacBand(1.0)).toBe("yellow");
    expect(ltvCacBand(0.5)).toBe("red");
    expect(ltvCacBand(null)).toBe("unknown");
  });
});

describe("configurable band thresholds (CTO-126)", () => {
  it("resolveThresholds falls back to defaults when no override row", () => {
    expect(resolveThresholds(null)).toEqual(DEFAULT_THRESHOLDS);
    expect(resolveThresholds(undefined)).toEqual(DEFAULT_THRESHOLDS);
  });

  it("resolveThresholds layers partial overrides ON TOP of defaults", () => {
    const resolved = resolveThresholds({ ltvCacGreen: 5.0 });
    expect(resolved.ltvCacGreen).toBe(5.0); // overridden
    expect(resolved.ltvCacYellow).toBe(DEFAULT_THRESHOLDS.ltvCacYellow); // default fallback
    expect(resolved.paybackGreen).toBe(DEFAULT_THRESHOLDS.paybackGreen);
    expect(resolved.paybackYellow).toBe(DEFAULT_THRESHOLDS.paybackYellow);
  });

  it("null fields in an override fall back to the default", () => {
    const resolved = resolveThresholds({ ltvCacGreen: null, paybackGreen: 6 });
    expect(resolved.ltvCacGreen).toBe(DEFAULT_THRESHOLDS.ltvCacGreen);
    expect(resolved.paybackGreen).toBe(6);
  });

  it("ltvCacBand applies a tenant override (green now needs >5)", () => {
    const t = resolveThresholds({ ltvCacGreen: 5.0, ltvCacYellow: 2.0 });
    // 3.5 was green under defaults; with the override it's only yellow.
    expect(ltvCacBand(3.5, t)).toBe("yellow");
    expect(ltvCacBand(3.5)).toBe("green"); // default arg unchanged (fallback)
    expect(ltvCacBand(6.0, t)).toBe("green");
    expect(ltvCacBand(1.5, t)).toBe("red"); // below the 2.0 yellow cutoff
  });

  it("paybackBand: lower is healthier; defaults are 12/18", () => {
    expect(paybackBand(10)).toBe("green");
    expect(paybackBand(12)).toBe("green");
    expect(paybackBand(15)).toBe("yellow");
    expect(paybackBand(18)).toBe("yellow");
    expect(paybackBand(24)).toBe("red");
    expect(paybackBand(null)).toBe("unknown");
  });

  it("paybackBand applies a tenant override and defaults remain the fallback", () => {
    const t = resolveThresholds({ paybackGreen: 6, paybackYellow: 10 });
    expect(paybackBand(8, t)).toBe("yellow"); // was green under defaults
    expect(paybackBand(8)).toBe("green"); // default arg fallback
    expect(paybackBand(5, t)).toBe("green");
    expect(paybackBand(12, t)).toBe("red");
  });
});
