// SPDX-License-Identifier: Apache-2.0
// CTO-392: every control-plane MUTATION requires the admin role.
//
// `canManage` was the policy from the start, but only the key and proxy routes ever asked it. Every
// other mutating route handler and server action resolved the tenant and wrote with the privileged
// gateway service token, so any organization member could change guardrail caps, LTV/CAC band
// thresholds, budgets, value-event mappings and connector credentials. The gateway cannot tell an
// admin from a member (it sees the service token and a resolved tenant, never the human), so the web
// server is the only place the distinction can be made.
//
// These tests are deliberately TABLE-DRIVEN over the full inventory rather than one case per
// endpoint: the failure mode this ticket fixes is not a broken check, it is a MISSING one, and a
// per-endpoint test suite has the same blind spot as the code it tests. A new mutating endpoint
// added to the table without the gate fails here; one added without a table row is caught by review
// against the inventory in the PR body.
//
// Clerk is mocked and TALLY_DEV_TENANT is cleared, because the dev escape hatch treats the local
// developer as an admin by design (lib/getTenant.ts) and would hide the very check being pinned.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authMock = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));
// Server actions revalidate after a write. There is no request context here, so the real thing
// throws; the gate runs long before it either way.
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

const TENANT_UUID = "3f8c1c2a-0b7e-4d3a-9a1b-77c0d5e2f011";
const ORG_ID = "org_test";

const originalDevTenant = process.env.TALLY_DEV_TENANT;
const originalToken = process.env.GATEWAY_SERVICE_TOKEN;
const originalIngest = process.env.TALLY_INGEST_URL;

let fetchCalls: Array<{ url: string; init?: RequestInit }>;

/** Everything except the org resolve, which is a READ and is allowed to happen before a refusal. */
function gatewayWrites() {
  return fetchCalls.filter((c) => !c.url.includes("/v1/tenant/by-clerk-org/"));
}

function asMember() {
  authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:member", userId: "user_1" });
}

function asAdmin() {
  authMock.mockResolvedValue({ orgId: ORG_ID, orgRole: "org:admin", userId: "user_1" });
}

beforeEach(() => {
  vi.resetModules();
  // Off, so getTenant() takes the product path through Clerk rather than the dev short-circuit.
  delete process.env.TALLY_DEV_TENANT;
  process.env.GATEWAY_SERVICE_TOKEN = "svc-token";
  // A deployment that runs the hosted proxy, so the proxy route reaches its write rather than 409.
  process.env.TALLY_INGEST_URL = "https://ingest.example.com";
  fetchCalls = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    fetchCalls.push({ url, init });
    if (url.includes("/v1/tenant/by-clerk-org/")) {
      return Promise.resolve(
        new Response(JSON.stringify({ tenant_id: TENANT_UUID, plan: "free" }), { status: 200 }),
      );
    }
    // A permissive stand-in for the control plane. What each endpoint does with the body is its own
    // test's business; these cases only care whether the call was attempted at all.
    return Promise.resolve(
      new Response(JSON.stringify({ config: { enabled: true } }), { status: 200 }),
    );
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

function req(body?: unknown, method = "POST"): Request {
  return new Request("http://localhost/x", {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

const params = { params: Promise.resolve({ id: "key-1" }) };

/** One mutating Route Handler. `call` invokes it with a body an admin would be allowed to send. */
interface RouteCase {
  name: string;
  call: () => Promise<Response>;
  /** False for the onboarding funnel, whose store is in-process rather than behind the gateway. */
  writesToGateway: boolean;
}

const ROUTES: RouteCase[] = [
  {
    name: "POST /api/keys",
    call: async () => (await import("./keys/route")).POST(req({ name: "k", scope: "write" })),
    writesToGateway: true,
  },
  {
    name: "DELETE /api/keys/[id]",
    call: async () => (await import("./keys/[id]/route")).DELETE(req(undefined, "DELETE"), params),
    writesToGateway: true,
  },
  {
    name: "POST /api/keys/[id]/rotate",
    call: async () => (await import("./keys/[id]/rotate/route")).POST(req(), params),
    writesToGateway: true,
  },
  {
    name: "POST /api/settings/proxy",
    call: async () => (await import("./settings/proxy/route")).POST(req({ enabled: true })),
    writesToGateway: true,
  },
  {
    name: "POST /api/guardrails",
    call: async () =>
      (await import("./guardrails/route")).POST(
        req({ id: "gr_x", scope: "a", mode: "warn", maxSteps: 10 }),
      ),
    writesToGateway: true,
  },
  {
    name: "POST /api/unit-economics/config",
    call: async () =>
      (await import("./unit-economics/config/route")).POST(
        req({ ltvCacGreen: 3, ltvCacYellow: 1, paybackGreen: 6, paybackYellow: 12 }),
      ),
    writesToGateway: true,
  },
  {
    name: "POST /api/features/value-events",
    call: async () =>
      (await import("./features/value-events/route")).POST(
        req({ feature: "chatbot", eventName: "paid_conversion" }),
      ),
    writesToGateway: true,
  },
  {
    name: "DELETE /api/features/value-events",
    call: async () =>
      (await import("./features/value-events/route")).DELETE(
        req({ feature: "chatbot" }, "DELETE"),
      ),
    writesToGateway: true,
  },
  {
    name: "POST /api/onboarding",
    call: async () => (await import("./onboarding/route")).POST(req({ stage: "first_trace" })),
    writesToGateway: false,
  },
];

/** One mutating server action. These answer with a result object, not an HTTP status. */
interface ActionCase {
  name: string;
  call: () => Promise<{ ok: boolean; error?: string }>;
}

const ACTIONS: ActionCase[] = [
  {
    name: "saveBudgetAction",
    call: async () =>
      (await import("@/app/settings/budgets/actions")).saveBudgetAction({
        budgetId: "b1",
        period: "month",
        amountDollars: "100",
        scopeKind: "tenant",
        scopeValue: "",
        startsOn: "2026-01-01",
        endsOn: "",
      }),
  },
  {
    name: "deleteBudgetAction",
    call: async () => (await import("@/app/settings/budgets/actions")).deleteBudgetAction("b1"),
  },
  {
    name: "toggleConnectorAction",
    call: async () => (await import("@/app/connectors/actions")).toggleConnectorAction("llm", true),
  },
  {
    name: "connectCostConnectorAction",
    call: async () =>
      (await import("@/app/connectors/costConnectorActions")).connectCostConnectorAction(
        "aws_cost_explorer",
        { credentials_ref: "arn:aws:iam::123456789012:role/r" },
      ),
  },
  {
    name: "disconnectCostConnectorAction",
    call: async () =>
      (await import("@/app/connectors/costConnectorActions")).disconnectCostConnectorAction(
        "aws_cost_explorer",
      ),
  },
  {
    name: "connectStripeAction",
    call: async () =>
      (await import("@/app/connectors/stripeActions")).connectStripeAction("whsec_x", null),
  },
  {
    name: "uploadRevenueCsvAction",
    call: async () =>
      (await import("@/app/connectors/revenueUploadActions")).uploadRevenueCsvAction(
        "account_id,period,amount,currency\na,2026-01,10,USD\n",
        "r.csv",
      ),
  },
  {
    name: "deleteRevenueUploadAction",
    call: async () =>
      (await import("@/app/connectors/revenueUploadActions")).deleteRevenueUploadAction("2026-01"),
  },
];

describe("CTO-392: mutating route handlers refuse a member", () => {
  it.each(ROUTES)("$name answers 403 and writes nothing", async ({ call }) => {
    asMember();

    const res = await call();

    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: expect.stringContaining("admin role required") });
    // The refusal has to happen BEFORE the gateway is touched. A 403 returned after the write
    // landed would be a reassuring status over a completed mutation.
    expect(gatewayWrites()).toHaveLength(0);
  });
});

describe("CTO-392: mutating route handlers still serve an admin", () => {
  it.each(ROUTES)("$name is not refused for an admin", async ({ call, writesToGateway }) => {
    asAdmin();

    const res = await call();

    expect(res.status).not.toBe(403);
    expect(res.status).toBeLessThan(400);
    if (writesToGateway) {
      expect(gatewayWrites().length).toBeGreaterThan(0);
    }
  });
});

describe("CTO-392: mutating server actions refuse a member", () => {
  // A server action is a callable endpoint, not just the handler behind a button: hiding the button
  // is presentation, and this is the check that actually holds.
  it.each(ACTIONS)("$name returns the admin-required refusal and writes nothing", async ({ call }) => {
    asMember();

    const result = await call();

    expect(result.ok).toBe(false);
    expect(result.error).toContain("admin role required");
    expect(gatewayWrites()).toHaveLength(0);
  });
});

describe("CTO-392: mutating server actions still let an admin through", () => {
  // Asserts the gate is passed and the write is attempted, not what the gateway makes of it: the
  // stub above is a stand-in, and each action's own handling of a real response is its own concern.
  it.each(ACTIONS)("$name reaches the gateway for an admin", async ({ call }) => {
    asAdmin();

    await call().catch(() => undefined);

    expect(gatewayWrites().length).toBeGreaterThan(0);
  });
});
