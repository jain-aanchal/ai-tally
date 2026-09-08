// SPDX-License-Identifier: Apache-2.0
// Admin gating on the /api/keys proxy (Initiative 1, §6/§9). The gateway sees only the service token
// and a resolved tenant; it does not know the human's role. That makes the web server the single
// policy enforcement point, so these tests pin it: a `member` must not be able to mint an ingest key,
// and no request must reach the gateway when the role check fails.
//
// Clerk is mocked and TALLY_DEV_TENANT is cleared per test, because the dev escape hatch treats the
// local developer as an admin by design and would hide the check the product path relies on.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authMock = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

const TENANT_UUID = "3f8c1c2a-0b7e-4d3a-9a1b-77c0d5e2f011";
const ORG_ID = "org_test";

const originalDevTenant = process.env.TALLY_DEV_TENANT;
const originalToken = process.env.GATEWAY_SERVICE_TOKEN;

/** Gateway responses the route's own fetch calls consume. Recorded so we can assert none happened. */
let fetchCalls: Array<{ url: string; init?: RequestInit }>;

beforeEach(() => {
  vi.resetModules();
  // Leave the escape hatch OFF so getTenant() takes the product path through Clerk.
  delete process.env.TALLY_DEV_TENANT;
  process.env.GATEWAY_SERVICE_TOKEN = "svc-token";
  fetchCalls = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    fetchCalls.push({ url, init });
    if (url.includes("/v1/tenant/by-clerk-org/")) {
      return Promise.resolve(
        new Response(JSON.stringify({ tenant_id: TENANT_UUID, plan: "free" }), { status: 200 }),
      );
    }
    if (init?.method === "POST") {
      return Promise.resolve(
        new Response(JSON.stringify({ id: "key-1", token: "tally_sk_live_secret" }), {
          status: 201,
        }),
      );
    }
    return Promise.resolve(new Response(JSON.stringify({ keys: [] }), { status: 200 }));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  authMock.mockReset();
  if (originalDevTenant === undefined) delete process.env.TALLY_DEV_TENANT;
  else process.env.TALLY_DEV_TENANT = originalDevTenant;
  if (originalToken === undefined) delete process.env.GATEWAY_SERVICE_TOKEN;
  else process.env.GATEWAY_SERVICE_TOKEN = originalToken;
});

function mintRequest(): Request {
  return new Request("http://localhost/api/keys", {
    method: "POST",
    body: JSON.stringify({ name: "prod ingest", scope: "write" }),
  });
}

describe("POST /api/keys admin gating", () => {
  it("refuses a member with 403 and never calls the gateway keys endpoint", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:member", userId: "user_1" });
    const { POST } = await import("./route");

    const res = await POST(mintRequest());

    expect(res.status).toBe(403);
    // The org resolve is allowed; the MINT must not have been attempted.
    expect(fetchCalls.some((c) => c.init?.method === "POST")).toBe(false);
  });

  it("lets an admin mint and forwards the service token plus the resolved tenant UUID", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
    const { POST } = await import("./route");

    const res = await POST(mintRequest());

    expect(res.status).toBe(201);
    const mint = fetchCalls.find((c) => c.init?.method === "POST");
    expect(mint).toBeDefined();
    const headers = mint?.init?.headers as Record<string, string>;
    expect(headers["x-tenant-id"]).toBe(TENANT_UUID);
    expect(headers.authorization).toBe("Bearer svc-token");
    expect(headers["x-clerk-user-id"]).toBe("user_1");
  });
});

describe("GET /api/keys", () => {
  it("is readable by a member: key metadata carries no secret (§9)", async () => {
    authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:member", userId: "user_1" });
    const { GET } = await import("./route");

    const res = await GET();

    expect(res.status).toBe(200);
    const listed = fetchCalls.find((c) => c.url.endsWith("/v1/tenant/keys"));
    expect(listed).toBeDefined();
    expect((listed?.init?.headers as Record<string, string>)["x-tenant-id"]).toBe(TENANT_UUID);
  });
});
