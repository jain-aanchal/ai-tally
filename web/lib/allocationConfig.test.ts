// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { DEFAULT_ALLOCATION_RULE } from "./allocation";
import { fallbackSetting, settingFromApi } from "./allocationConfig";

describe("settingFromApi (CTO-193 wire mapping)", () => {
  it("carries a tenant's configured rule through, with who set it and when", () => {
    expect(
      settingFromApi({
        allocation_rule: "even_split",
        configured: true,
        config: { updated_at: "2026-03-04T10:00:00+00:00", updated_by: "finance@acme.test" },
      }),
    ).toEqual({
      rule: "even_split",
      source: "tenant",
      updatedAt: "2026-03-04T10:00:00+00:00",
      updatedBy: "finance@acme.test",
    });
  });

  it("reports the default as the DEFAULT, not as a choice the tenant made", () => {
    // The state every tenant is in today. The page says "the product default, nobody configured
    // one", which is a weaker and truer claim than presenting it as a decision.
    const setting = settingFromApi({
      allocation_rule: "pro_rata_direct",
      configured: false,
      config: null,
    });
    expect(setting.rule).toBe(DEFAULT_ALLOCATION_RULE);
    expect(setting.source).toBe("default");
  });

  it("distinguishes an unreadable config from an unconfigured tenant", () => {
    // Both apply the default rule, but they are different claims about the tenant's configuration
    // and the page words them differently. Collapsing them would assert something we never checked.
    expect(settingFromApi(null)).toEqual(fallbackSetting("unavailable"));
    expect(settingFromApi(null).source).not.toBe("default");
  });

  it("refuses a rule this dashboard cannot apply rather than pretending it was chosen", () => {
    // A gateway newer than the web app. Applying the default is the only option, but reporting it
    // as the tenant's rule would put a name on screen that did not produce the numbers beside it.
    const setting = settingFromApi({ allocation_rule: "pro_rata_tokens", configured: true });
    expect(setting.rule).toBe(DEFAULT_ALLOCATION_RULE);
    expect(setting.source).toBe("unavailable");
  });

  it("treats a malformed rule field the same way", () => {
    expect(settingFromApi({ allocation_rule: 7, configured: true }).source).toBe("unavailable");
    expect(settingFromApi({}).source).toBe("unavailable");
  });

  it("drops non-string audit fields instead of rendering them", () => {
    const setting = settingFromApi({
      allocation_rule: "pro_rata_direct",
      configured: true,
      config: { updated_at: 1234, updated_by: null },
    });
    expect(setting.source).toBe("tenant");
    expect(setting.updatedAt).toBeNull();
    expect(setting.updatedBy).toBeNull();
  });

  it("always yields a usable rule, on every path", () => {
    for (const body of [null, {}, { allocation_rule: "nope" }, { configured: true }]) {
      expect(settingFromApi(body)).toHaveProperty("rule", DEFAULT_ALLOCATION_RULE);
    }
  });
});
