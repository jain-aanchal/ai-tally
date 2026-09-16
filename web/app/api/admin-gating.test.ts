// SPDX-License-Identifier: Apache-2.0
/// <reference types="vite/client" />
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
// per-endpoint test suite has the same blind spot as the code it tests.
//
// The table itself is therefore DERIVED, not maintained by hand. A hand-written list of 34 cases
// has exactly the blind spot it is meant to guard: a route added without a gate is also a route
// nobody adds a row for, and the suite stays green. So the mutating handlers are globbed off disk
// and each one must appear in the table or in a named allowlist with its reason. Adding an ungated
// endpoint now fails this file rather than passing it silently.
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

/** The HTTP methods that change tenant state. A GET handler is a read and is never gated. */
const MUTATING_METHODS = ["POST", "PUT", "PATCH", "DELETE"] as const;

/**
 * Mutating endpoints that are deliberately NOT admin-gated, each with the reason.
 *
 * An allowlist rather than an omission: leaving one out of the table would read as an oversight,
 * and the next person to look would have to re-derive why it is safe.
 */
const UNGATED_ROUTES: Record<string, string> = {
  "/api/estimate":
    "a replay what-if projection: pure computation over the captured corpus, writes no tenant state",
  "/api/webhooks/clerk":
    "Clerk's provisioning webhook, authenticated by svix signature. It is machine-to-machine and has no Clerk session or org to check a role against",
  "/api/onboarding":
    "internal activation telemetry, not spend-governing config. The client fires it once and ignores the answer, so gating it would silently drop any milestone a member reached first rather than protecting anything",
};

/** Server actions that are deliberately not gated, same rule as UNGATED_ROUTES. */
const UNGATED_ACTIONS: Record<string, string> = {
  lookupAccountAction:
    "a lookup: it hashes an account id and returns the hashes, writing no tenant state",
};

/** One mutating Route Handler. `call` invokes it with a body an admin would be allowed to send. */
interface RouteCase {
  name: string;
  call: () => Promise<Response>;
}

const ROUTES: RouteCase[] = [
  {
    name: "POST /api/keys",
    call: async () => (await import("./keys/route")).POST(req({ name: "k", scope: "write" })),  },
  {
    name: "DELETE /api/keys/[id]",
    call: async () => (await import("./keys/[id]/route")).DELETE(req(undefined, "DELETE"), params),  },
  {
    name: "POST /api/keys/[id]/rotate",
    call: async () => (await import("./keys/[id]/rotate/route")).POST(req(), params),  },
  {
    name: "POST /api/settings/proxy",
    call: async () => (await import("./settings/proxy/route")).POST(req({ enabled: true })),  },
  {
    name: "POST /api/guardrails",
    call: async () =>
      (await import("./guardrails/route")).POST(
        req({ id: "gr_x", scope: "a", mode: "warn", maxSteps: 10 }),
      ),  },
  {
    name: "POST /api/unit-economics/config",
    call: async () =>
      (await import("./unit-economics/config/route")).POST(
        req({ ltvCacGreen: 3, ltvCacYellow: 1, paybackGreen: 6, paybackYellow: 12 }),
      ),  },
  {
    name: "POST /api/features/value-events",
    call: async () =>
      (await import("./features/value-events/route")).POST(
        req({ feature: "chatbot", eventName: "paid_conversion" }),
      ),  },
  {
    name: "DELETE /api/features/value-events",
    call: async () =>
      (await import("./features/value-events/route")).DELETE(
        req({ feature: "chatbot" }, "DELETE"),
      ),  },
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

// The tables above are checked against the tree, not trusted.
//
// `import.meta.glob` is resolved by vite from the files on disk, so these see what was actually
// added rather than what this suite remembers.
const ROUTE_MODULES = import.meta.glob("./**/route.ts");
const APP_SOURCES = import.meta.glob("../**/*.ts", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** "./features/value-events/route.ts" -> "/api/features/value-events". */
function endpointFor(globPath: string): string {
  return `/api${globPath.replace(/^\./, "").replace(/\/route\.ts$/, "")}`;
}

describe("CTO-392: the inventory above covers everything on disk", () => {
  // A derivation that silently matched nothing would pass forever, which is the failure it exists
  // to prevent. Pin that it really sees the tree.
  it("actually finds the route and source trees", () => {
    expect(Object.keys(ROUTE_MODULES).length).toBeGreaterThan(10);
    expect(Object.values(APP_SOURCES).some((s) => /^\s*"use server";/m.test(s))).toBe(true);
  });

  it("every mutating route handler is in the table or explicitly allowlisted", async () => {
    const ungated: string[] = [];
    for (const [globPath, load] of Object.entries(ROUTE_MODULES)) {
      const endpoint = endpointFor(globPath);
      // Allowlisted endpoints are skipped WITHOUT importing: why they are open has nothing to do
      // with which methods they export, and this check should not break when one is edited.
      if (endpoint in UNGATED_ROUTES) continue;
      const mod = (await load()) as Record<string, unknown>;
      for (const method of MUTATING_METHODS) {
        if (typeof mod[method] !== "function") continue;
        const name = `${method} ${endpoint}`;
        if (!ROUTES.some((r) => r.name === name)) ungated.push(name);
      }
    }
    expect(ungated).toEqual([]);
  });

  it("every exported server action is in the table or explicitly allowlisted", () => {
    const ungated: string[] = [];
    for (const [path, source] of Object.entries(APP_SOURCES)) {
      // The directive has to START a line. A comment or a regex that merely mentions it, as this
      // file does, is not a server-action module.
      if (!/^\s*"use server";/m.test(source)) continue;
      for (const [, action] of source.matchAll(/export\s+async\s+function\s+(\w+)/g)) {
        if (action in UNGATED_ACTIONS) continue;
        if (!ACTIONS.some((a) => a.name === action)) ungated.push(`${path}: ${action}`);
      }
    }
    expect(ungated).toEqual([]);
  });
});

// CTO-393: gating a mutation ABOVE its try left the tenant resolve, which is itself a gateway call,
// with no honest failure path. Every one of these threw ECONNREFUSED out of the handler, which Next
// answers as a bare 500, telling the customer nothing about whether their change was stored.
// Reproduced against this branch before the fix was written.
describe("CTO-393: a mutation whose tenant cannot be resolved refuses honestly", () => {
  it.each(ROUTES)("$name answers 503 rather than throwing", async ({ call }) => {
    asAdmin();
    // The org resolve fails too, which is what an unreachable control plane actually looks like.
    vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
      fetchCalls.push({ url: String(input) });
      return Promise.reject(new Error("ECONNREFUSED"));
    });

    const res = await call();

    expect(res.status).toBe(503);
    const body = (await res.json()) as { error?: string; persisted?: boolean };
    expect(body.error).toContain("control plane is unreachable");
    // Stated, not inferred from the status: no caller should have to guess whether a failed
    // request left something behind.
    expect(body.persisted).toBe(false);
    expect(gatewayWrites()).toHaveLength(0);
  });
});

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
  it.each(ROUTES)("$name is not refused for an admin", async ({ call }) => {
    asAdmin();

    const res = await call();

    expect(res.status).not.toBe(403);
    expect(res.status).toBeLessThan(400);
    expect(gatewayWrites().length).toBeGreaterThan(0);
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
