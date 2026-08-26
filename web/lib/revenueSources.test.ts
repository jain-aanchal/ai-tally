// SPDX-License-Identifier: Apache-2.0
// CTO-194: the revenue policy that replaced the hardcoded Source='stripe' filter.

import { describe, expect, it } from "vitest";

import {
  DEFAULT_REVENUE_POLICY,
  REFUND_VALUE_TYPE,
  policyFromApi,
  positiveValueTypes,
  revenueSourceFilter,
  type RevenueSourceConfigApi,
} from "./revenueSources";

function api(overrides: Partial<RevenueSourceConfigApi> = {}): RevenueSourceConfigApi {
  return {
    revenue_sources: null,
    include_mrr: true,
    created_at: null,
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

describe("policyFromApi", () => {
  it("uses the defaults when the tenant has no config row", () => {
    // The whole point of the migration: an unconfigured tenant must keep working.
    expect(policyFromApi(null)).toEqual(DEFAULT_REVENUE_POLICY);
    expect(policyFromApi(null).sources).toBeNull();
  });

  it("lowercases and de-dupes the configured sources", () => {
    const p = policyFromApi(api({ revenue_sources: ["Stripe", " stripe ", "Chargebee"] }));
    expect(p.sources).toEqual(["stripe", "chargebee"]);
  });

  it("treats an unusable source list as all-sources rather than no-revenue", () => {
    // Blanking the dashboard on bad config is the exact bug this replaced.
    expect(policyFromApi(api({ revenue_sources: [] })).sources).toBeNull();
    expect(policyFromApi(api({ revenue_sources: ["  "] })).sources).toBeNull();
  });

  it("honours include_mrr, defaulting to true", () => {
    expect(policyFromApi(api({ include_mrr: false })).includeMrr).toBe(false);
    expect(policyFromApi(api()).includeMrr).toBe(true);
  });
});

describe("positiveValueTypes", () => {
  it("counts monetary and mrr by default, never count or refund", () => {
    const types = positiveValueTypes(DEFAULT_REVENUE_POLICY);
    expect(types).toEqual(["monetary", "mrr"]);
    expect(types).not.toContain("count");
    expect(types).not.toContain(REFUND_VALUE_TYPE);
  });

  it("drops mrr when the tenant opted out", () => {
    expect(positiveValueTypes({ sources: null, includeMrr: false })).toEqual(["monetary"]);
  });
});

describe("revenueSourceFilter", () => {
  it("emits no filter when every source counts", () => {
    const f = revenueSourceFilter(DEFAULT_REVENUE_POLICY, "b");
    expect(f.sql).toBe("");
    expect(f.params).toEqual({});
  });

  it("binds the source list as a parameter rather than interpolating it", () => {
    // Source values are tenant-supplied strings; they must never be spliced into SQL.
    const f = revenueSourceFilter({ sources: ["chargebee"], includeMrr: true }, "b");
    expect(f.sql).toBe("AND lower(b.Source) IN {revenueSources:Array(String)}");
    expect(f.params).toEqual({ revenueSources: ["chargebee"] });
    expect(f.sql).not.toContain("chargebee");
  });
});
