// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { provisioningFailureReason } from "@/lib/provisioning";

import { GET } from "./route";

describe("GET /api/tenant/provisioning-status", () => {
  it("reports ready when the tenant resolves", async () => {
    // The suite runs on the dev escape hatch, so resolution succeeds without Clerk.
    const body = (await (await GET()).json()) as { state: string; reason: string | null };
    expect(body.state).toBe("ready");
    expect(body.reason).toBeNull();
  });
});

describe("provisioningFailureReason", () => {
  // The status code is the one detail worth keeping; the exception itself is not fit to render.
  it("keeps the status code and nothing else from the error", () => {
    const reason = provisioningFailureReason(new Error("by-clerk-org resolve failed: HTTP 503"));
    expect(reason).toContain("HTTP 503");
    expect(reason).not.toContain("by-clerk-org");
  });

  it("falls back to a generic sentence rather than echoing an unknown error message", () => {
    const reason = provisioningFailureReason(new Error("ECONNREFUSED 10.0.0.4:8080"));
    expect(reason).not.toContain("ECONNREFUSED");
    expect(reason).toMatch(/could not be reached/);
  });
});
