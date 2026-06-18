// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import type { CacPeriod } from "./cac";
import {
  blendedCac,
  costPerUser,
  fullyLoadedCac,
  ltv,
  ltvCacBand,
  ltvOverCac,
  marginPct,
  marginPerUser,
  marketingCac,
  paybackMonths,
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
