// SPDX-License-Identifier: Apache-2.0
// CTO-145: the gateway-backed economics mapping — ARPA + gross margin light up payback/LTV, and a
// period missing either field stays honest-null ("—").

import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_RETENTION_MONTHS, queryCacPeriods } from "./cac";
import { ltv, marginPerUser, paybackMonths } from "./unitEconomics";

function apiPeriod(overrides: Record<string, unknown> = {}) {
  return {
    period_start: "2026-05-01",
    period_end: "2026-05-31",
    currency: "USD",
    paid_spend_micro_usd: 42_000_000_000,
    sales_spend_micro_usd: 28_000_000_000,
    content_spend_micro_usd: 9_000_000_000,
    overhead_micro_usd: 31_000_000_000,
    new_customers_paid: 38,
    new_customers_total: 61,
    notes: null,
    closed_at: null,
    locked: false,
    arpa_micro_usd: 220_000_000,
    gross_margin_pct: 0.78,
    ...overrides,
  };
}

function mockGateway(periods: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ periods }) }) as unknown as Response),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("queryCacPeriods economics mapping (CTO-145)", () => {
  it("maps ARPA + gross margin into the economics map and yields non-null payback/LTV", async () => {
    mockGateway([apiPeriod()]);
    const { periods, economics } = await queryCacPeriods();
    expect(periods).toHaveLength(1);

    const econ = economics["2026-05-01"];
    expect(econ).toBeDefined();
    expect(econ.arpaMicroUsd).toBe(220_000_000);
    expect(econ.grossMarginPct).toBe(0.78);
    expect(econ.retentionMonths).toBe(DEFAULT_RETENTION_MONTHS);

    // A fully-entered month lights up payback + LTV (both non-null).
    const margin = marginPerUser(econ.arpaMicroUsd, econ.arpaMicroUsd * (1 - econ.grossMarginPct));
    expect(paybackMonths(1_000_000_000, margin)).not.toBeNull();
    expect(ltv(margin, econ.retentionMonths)).not.toBeNull();
    expect(ltv(margin, econ.retentionMonths)).toBeGreaterThan(0);
  });

  it("omits the economics record when ARPA is missing (stays honest-null)", async () => {
    mockGateway([apiPeriod({ arpa_micro_usd: null })]);
    const { economics } = await queryCacPeriods();
    expect(economics["2026-05-01"]).toBeUndefined();
  });

  it("omits the economics record when gross margin is missing (stays honest-null)", async () => {
    mockGateway([apiPeriod({ gross_margin_pct: null })]);
    const { economics } = await queryCacPeriods();
    expect(economics["2026-05-01"]).toBeUndefined();
  });
});
