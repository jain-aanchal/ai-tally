// SPDX-License-Identifier: Apache-2.0
// Clerk provisioning webhook (Initiative 1, §4). svix is mocked so no real signing secret is needed;
// the tests assert the route rejects a bad signature, forwards a verified organization.created to
// the gateway's provision endpoint, and acks other event types without forwarding.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const verify = vi.fn();
vi.mock("svix", () => ({
  Webhook: class {
    verify(...args: unknown[]) {
      return verify(...args);
    }
  },
}));

import { POST } from "./route";

function req(body: unknown, withHeaders = true): Request {
  const headers: Record<string, string> = withHeaders
    ? { "svix-id": "id", "svix-timestamp": "ts", "svix-signature": "sig" }
    : {};
  return new Request("http://localhost/api/webhooks/clerk", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

describe("POST /api/webhooks/clerk", () => {
  beforeEach(() => {
    process.env.CLERK_WEBHOOK_SIGNING_SECRET = "whsec_test";
    verify.mockReset();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("rejects an invalid svix signature with 401 and never forwards", async () => {
    verify.mockImplementation(() => {
      throw new Error("bad signature");
    });
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const res = await POST(req({ type: "organization.created", data: { id: "org_1" } }));
    expect(res.status).toBe(401);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("forwards a verified organization.created to the gateway provision endpoint", async () => {
    verify.mockReturnValue({ type: "organization.created", data: { id: "org_1", name: "Acme" } });
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ tenant_id: "t", created: true }), { status: 200 }));
    const res = await POST(req({}));
    expect(res.status).toBe(200);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain("/v1/tenant/provision");
    expect(JSON.parse(String((init as RequestInit).body))).toMatchObject({
      clerk_org_id: "org_1",
      name: "Acme",
    });
  });

  it("acks a non-org-created event without forwarding", async () => {
    verify.mockReturnValue({ type: "user.created", data: { id: "user_1" } });
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const res = await POST(req({}));
    expect(res.status).toBe(200);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns 5xx when the gateway provision fails so Clerk retries", async () => {
    verify.mockReturnValue({ type: "organization.created", data: { id: "org_1", name: "Acme" } });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 500 }));
    const res = await POST(req({}));
    expect(res.status).toBeGreaterThanOrEqual(500);
  });
});
