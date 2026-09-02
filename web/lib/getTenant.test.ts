// SPDX-License-Identifier: Apache-2.0
// getTenant dev escape hatch + header helpers (Initiative 1, §7/§10). The vitest env sets
// TALLY_DEV_TENANT (see vitest.config.ts), so these exercise the short-circuit that lets the app run
// with no Clerk account: getTenant never imports Clerk and resolves to the pinned tenant.
import { afterEach, describe, expect, it } from "vitest";

import {
  canManage,
  controlPlaneHeaders,
  devTenant,
  getTenant,
  resolveTenantId,
  serviceTokenHeader,
} from "./getTenant";

describe("getTenant dev escape hatch", () => {
  it("short-circuits to the pinned dev tenant without consulting Clerk", async () => {
    expect(devTenant()).toBe("local-dev");
    const t = await getTenant();
    expect(t).toEqual({ tenantId: "local-dev", orgId: null, orgRole: null });
    expect(await resolveTenantId()).toBe("local-dev");
  });

  it("treats dev as admin so local key management works", async () => {
    expect(canManage(await getTenant())).toBe(true);
  });
});

describe("control-plane headers", () => {
  const original = process.env.GATEWAY_SERVICE_TOKEN;
  afterEach(() => {
    if (original === undefined) delete process.env.GATEWAY_SERVICE_TOKEN;
    else process.env.GATEWAY_SERVICE_TOKEN = original;
  });

  it("carries the tenant and, when set, the service token", () => {
    process.env.GATEWAY_SERVICE_TOKEN = "svc-123";
    expect(serviceTokenHeader()).toEqual({ authorization: "Bearer svc-123" });
    expect(controlPlaneHeaders("t-uuid", { "content-type": "application/json" })).toEqual({
      "x-tenant-id": "t-uuid",
      authorization: "Bearer svc-123",
      "content-type": "application/json",
    });
  });

  it("omits the token header when no service token is configured", () => {
    delete process.env.GATEWAY_SERVICE_TOKEN;
    expect(serviceTokenHeader()).toEqual({});
    expect(controlPlaneHeaders("t-uuid")).toEqual({ "x-tenant-id": "t-uuid" });
  });
});
