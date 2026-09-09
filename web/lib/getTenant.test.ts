// SPDX-License-Identifier: Apache-2.0
// getTenant dev escape hatch + header helpers (Initiative 1, §7/§10). The vitest env sets
// TALLY_DEV_TENANT (see vitest.config.ts), so these exercise the short-circuit that lets the app run
// with no Clerk account: getTenant never imports Clerk and resolves to the pinned tenant.
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  TenantNotProvisionedError,
  canManage,
  controlPlaneHeaders,
  devTenant,
  getTenant,
  resolveTenantId,
  serviceTokenHeader,
} from "./getTenant";

// The product path needs Clerk to answer with an active org. The real module is never installed in
// this suite's environment (no keys), and getTenant imports it lazily, so a factory mock is enough.
vi.mock("@clerk/nextjs/server", () => ({
  auth: async () => ({ orgId: "org_race", orgRole: "org:admin", userId: "user_1" }),
}));

describe("getTenant dev escape hatch", () => {
  it("short-circuits to the pinned dev tenant without consulting Clerk", async () => {
    // Pinned in vitest.config.ts. A UUID, not the name `local-dev`: the canonical TenantId is the
    // tenant UUID (Initiative 1, §8) and this value is bound into the ClickHouse read filter.
    const DEV_TENANT = "00000000-0000-0000-0000-000000000000";
    expect(devTenant()).toBe(DEV_TENANT);
    const t = await getTenant();
    expect(t).toEqual({ tenantId: DEV_TENANT, orgId: null, orgRole: null });
    expect(await resolveTenantId()).toBe(DEV_TENANT);
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

// #358: the provisioning race. Clerk's organization.created webhook is asynchronous and the
// post-signup redirect is not, so a browser that wins the race asks for an org the gateway has
// never heard of. The gateway answers 404 deliberately. That 404 is transient and self-heals; any
// other failure does not, and the two must not reach a customer as the same screen.
describe("provisioning race resolution (product path)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("throws TenantNotProvisionedError on the deliberate 404", async () => {
    vi.stubEnv("TALLY_DEV_TENANT", "");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }),
    );
    await expect(getTenant()).rejects.toBeInstanceOf(TenantNotProvisionedError);
  });

  it("keeps every other resolution failure a plain error carrying its status", async () => {
    vi.stubEnv("TALLY_DEV_TENANT", "");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );
    const err = await getTenant().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(TenantNotProvisionedError);
    expect((err as Error).message).toMatch(/HTTP 503/);
  });
});
