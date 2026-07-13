// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { resolveThresholds, DEFAULT_THRESHOLDS } from "./unitEconomics";
import { overridesFromApi } from "./unitEconomicsConfig";

describe("overridesFromApi (CTO-126 wire mapping)", () => {
  it("maps the gateway snake_case config to camelCase overrides", () => {
    const overrides = overridesFromApi({
      ltv_cac_green_threshold: 4.0,
      ltv_cac_yellow_threshold: 1.5,
      payback_months_green: 8,
      payback_months_yellow: 16,
      created_at: null,
      updated_at: null,
      updated_by: "finance@acme.test",
    });
    expect(overrides).toEqual({
      ltvCacGreen: 4.0,
      ltvCacYellow: 1.5,
      paybackGreen: 8,
      paybackYellow: 16,
    });
  });

  it("returns null when the tenant has no config row → resolveThresholds uses defaults", () => {
    const overrides = overridesFromApi(null);
    expect(overrides).toBeNull();
    expect(resolveThresholds(overrides)).toEqual(DEFAULT_THRESHOLDS);
  });

  it("a mapped override resolves ON TOP of defaults", () => {
    const overrides = overridesFromApi({
      ltv_cac_green_threshold: 5.0,
      ltv_cac_yellow_threshold: 2.0,
      payback_months_green: 6,
      payback_months_yellow: 12,
      created_at: null,
      updated_at: null,
      updated_by: null,
    });
    expect(resolveThresholds(overrides)).toEqual({
      ltvCacGreen: 5.0,
      ltvCacYellow: 2.0,
      paybackGreen: 6,
      paybackYellow: 12,
    });
  });
});
