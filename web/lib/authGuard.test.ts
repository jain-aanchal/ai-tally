// SPDX-License-Identifier: Apache-2.0
// The production guard on the dev escape hatch (CTO-268).
//
// The guard is pure over an env bag, so these assert the decision table directly instead of mutating
// process.env. The BOOT behaviour it feeds (instrumentation.ts printing and exiting non-zero) is
// exercised against a real production build, not here: see the PR body.
import { describe, expect, it, vi } from "vitest";

import {
  ALLOW_INSECURE_ENV,
  DEV_TENANT_ENV,
  InsecureAuthConfigError,
  assertAuthConfig,
  checkAuthConfig,
} from "./authGuard";

const TENANT = "11111111-2222-3333-4444-555555555555";

describe("checkAuthConfig", () => {
  it("refuses production + dev tenant with no opt-in", () => {
    const v = checkAuthConfig({ NODE_ENV: "production", TALLY_DEV_TENANT: TENANT });
    expect(v.kind).toBe("refuse");
  });

  it("allows production + dev tenant + explicit opt-in", () => {
    const v = checkAuthConfig({
      NODE_ENV: "production",
      TALLY_DEV_TENANT: TENANT,
      TALLY_ALLOW_INSECURE_NO_AUTH: "1",
    });
    expect(v.kind).toBe("insecure-allowed");
  });

  it("allows development + dev tenant (`make up`)", () => {
    const v = checkAuthConfig({ NODE_ENV: "development", TALLY_DEV_TENANT: TENANT });
    expect(v).toEqual({ kind: "ok" });
  });

  it("allows the test env + dev tenant (the vitest suite pins one)", () => {
    const v = checkAuthConfig({ NODE_ENV: "test", TALLY_DEV_TENANT: TENANT });
    expect(v).toEqual({ kind: "ok" });
  });

  it("allows production with neither variable (the product path)", () => {
    expect(checkAuthConfig({ NODE_ENV: "production" })).toEqual({ kind: "ok" });
  });

  it("allows the production BUILD phase, which CI runs through the escape hatch", () => {
    const v = checkAuthConfig({
      NODE_ENV: "production",
      NEXT_PHASE: "phase-production-build",
      TALLY_DEV_TENANT: TENANT,
    });
    expect(v).toEqual({ kind: "ok" });
  });

  it("treats an empty or whitespace dev tenant as unset (the manifests set it to \"\")", () => {
    expect(checkAuthConfig({ NODE_ENV: "production", TALLY_DEV_TENANT: "" })).toEqual({ kind: "ok" });
    expect(checkAuthConfig({ NODE_ENV: "production", TALLY_DEV_TENANT: "   " })).toEqual({
      kind: "ok",
    });
  });

  it("accepts the same opt-in spellings the demo shell scripts accept", () => {
    for (const value of ["1", "true", "TRUE", "yes", "on", " On "]) {
      expect(
        checkAuthConfig({
          NODE_ENV: "production",
          TALLY_DEV_TENANT: TENANT,
          TALLY_ALLOW_INSECURE_NO_AUTH: value,
        }).kind,
      ).toBe("insecure-allowed");
    }
  });

  it("does not accept a falsey opt-in as consent", () => {
    for (const value of ["", "0", "false", "no", "off", "maybe"]) {
      expect(
        checkAuthConfig({
          NODE_ENV: "production",
          TALLY_DEV_TENANT: TENANT,
          TALLY_ALLOW_INSECURE_NO_AUTH: value,
        }).kind,
      ).toBe("refuse");
    }
  });
});

describe("the refusal message", () => {
  const message = (() => {
    const v = checkAuthConfig({ NODE_ENV: "production", TALLY_DEV_TENANT: TENANT });
    if (v.kind !== "refuse") throw new Error("expected a refusal");
    return v.message;
  })();

  it("names both variables and the pinned tenant", () => {
    expect(message).toContain(DEV_TENANT_ENV);
    expect(message).toContain(ALLOW_INSECURE_ENV);
    expect(message).toContain(TENANT);
  });

  it("states the consequence and gives a fix for each of the two situations", () => {
    expect(message).toContain("disables authentication");
    expect(message).toContain("CLERK_SECRET_KEY");
    expect(message).toContain("deploy/demo/");
  });
});

describe("assertAuthConfig", () => {
  it("throws InsecureAuthConfigError on the one-variable production case", () => {
    expect(() => assertAuthConfig({ NODE_ENV: "production", TALLY_DEV_TENANT: TENANT })).toThrow(
      InsecureAuthConfigError,
    );
  });

  it("warns but returns on the opted-in case, so the demo is loud rather than silent", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      assertAuthConfig({
        NODE_ENV: "production",
        TALLY_DEV_TENANT: TENANT,
        TALLY_ALLOW_INSECURE_NO_AUTH: "1",
      });
      expect(warn).toHaveBeenCalledOnce();
      expect(warn.mock.calls[0][0]).toContain("NO AUTHENTICATION");
    } finally {
      warn.mockRestore();
    }
  });

  it("stays silent on a healthy configuration", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      assertAuthConfig({ NODE_ENV: "production" });
      expect(warn).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });
});
