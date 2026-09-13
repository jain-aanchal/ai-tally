// SPDX-License-Identifier: Apache-2.0
// The hosted-proxy switch's server seam (0033). As with /api/keys, the gateway sees only the service
// token and a resolved tenant, so the admin check lives here and these tests pin it: a member cannot
// change whether the organization's production LLM traffic is accepted, and a refused or malformed
// request never reaches the gateway.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authMock = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

const TENANT_UUID = "3f8c1c2a-0b7e-4d3a-9a1b-77c0d5e2f011";
const ORG_ID = "org_test";

const originalDevTenant = process.env.TALLY_DEV_TENANT;
const originalToken = process.env.GATEWAY_SERVICE_TOKEN;
const originalIngest = process.env.TALLY_INGEST_URL;

let fetchCalls: Array<{ url: string; init?: RequestInit }>;
/** What the gateway's GET answers with, so the unreadable case can be exercised. */
let configStatus: number;

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
}

beforeEach(() => {
  vi.resetModules();
  delete process.env.TALLY_DEV_TENANT;
  process.env.GATEWAY_SERVICE_TOKEN = "svc-token";
  // A deployment that runs the hosted proxy. The no-proxy case unsets it per test.
  process.env.TALLY_INGEST_URL = "https://ingest.example.com";
  fetchCalls = [];
  configStatus = 200;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    fetchCalls.push({ url, init });
    if (url.includes("/v1/tenant/by-clerk-org/")) {
      return json({ tenant_id: TENANT_UUID, plan: "free" });
    }
    if (url.endsWith("/v1/tenant/proxy/config") && init?.method === "POST") {
      const sent = JSON.parse(String(init.body)) as { enabled: boolean };
      return json({ tenant_id: TENANT_UUID, config: { enabled: sent.enabled, updated_at: null } });
    }
    if (url.endsWith("/v1/tenant/proxy/config")) {
      return configStatus === 200
        ? json({ tenant_id: TENANT_UUID, config: { enabled: false, updated_at: null } })
        : json({ detail: "boom" }, configStatus);
    }
    return json({}, 404);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  authMock.mockReset();
  if (originalDevTenant === undefined) delete process.env.TALLY_DEV_TENANT;
  else process.env.TALLY_DEV_TENANT = originalDevTenant;
  if (originalToken === undefined) delete process.env.GATEWAY_SERVICE_TOKEN;
  else process.env.GATEWAY_SERVICE_TOKEN = originalToken;
  if (originalIngest === undefined) delete process.env.TALLY_INGEST_URL;
  else process.env.TALLY_INGEST_URL = originalIngest;
});

function setRequest(body: unknown): Request {
  return new Request("http://localhost/api/settings/proxy", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

const gatewayWrites = () =>
  fetchCalls.filter((c) => c.url.endsWith("/v1/tenant/proxy/config") && c.init?.method === "POST");

describe("POST /api/settings/proxy", () => {
  it("refuses a member with 403 and never writes to the gateway", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:member", userId: "user_1" });
    const { POST } = await import("./route");

    const res = await POST(setRequest({ enabled: true }));

    expect(res.status).toBe(403);
    expect(gatewayWrites()).toHaveLength(0);
  });

  it("lets an admin turn it on, forwarding the service token, tenant and who did it", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
    const { POST } = await import("./route");

    const res = await POST(setRequest({ enabled: true }));

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ enabled: true });
    const [write] = gatewayWrites();
    const headers = write.init?.headers as Record<string, string>;
    expect(headers["x-tenant-id"]).toBe(TENANT_UUID);
    expect(headers.authorization).toBe("Bearer svc-token");
    expect(headers["x-clerk-user-id"]).toBe("user_1");
    expect(JSON.parse(String(write.init?.body))).toEqual({ enabled: true });
  });

  it.each([["false"], [1], [null]])(
    "refuses a non-boolean enabled (%j) with 400 before it reaches the gateway",
    async (value) => {
      // "false" as a string is truthy almost everywhere. Coercing it would turn the proxy ON.
      authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
      const { POST } = await import("./route");

      const res = await POST(setRequest({ enabled: value }));

      expect(res.status).toBe(400);
      expect(gatewayWrites()).toHaveLength(0);
    },
  );
});

describe("GET /api/settings/proxy", () => {
  it("is readable by a member", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:member", userId: "user_1" });
    const { GET } = await import("./route");

    const res = await GET();

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ deployed: true, enabled: false });
  });

  it("reports an unreadable setting as null, never as off", async () => {
    // "Off" when the gateway could not answer would tell an admin their proxy is disabled while it may
    // be accepting traffic.
    configStatus = 500;
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
    const { GET } = await import("./route");

    const res = await GET();

    expect(await res.json()).toEqual({ deployed: true, enabled: null });
  });

  it("reports no deployment, and reads no setting, where no hosted proxy runs", async () => {
    delete process.env.TALLY_INGEST_URL;
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
    const { GET } = await import("./route");

    const res = await GET();

    expect(await res.json()).toEqual({ deployed: false, enabled: null });
    expect(fetchCalls.some((c) => c.url.endsWith("/v1/tenant/proxy/config"))).toBe(false);
  });
});

describe("POST /api/settings/proxy on a deployment without a hosted proxy (review of #385, finding 3)", () => {
  it("refuses with 409 and never writes, so no one is told proxies will start accepting keys", async () => {
    delete process.env.TALLY_INGEST_URL;
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
    const { POST } = await import("./route");

    const res = await POST(setRequest({ enabled: true }));

    expect(res.status).toBe(409);
    expect(gatewayWrites()).toHaveLength(0);
  });
});
