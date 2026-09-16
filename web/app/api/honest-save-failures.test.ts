// SPDX-License-Identifier: Apache-2.0
// CTO-393: a save that never reached the control plane must not be reported as a success.
//
// Three routes caught an unreachable gateway and answered HTTP 200 with `persisted: false`, while
// every caller checked only `res.ok`. A guardrail cap change that was never stored rendered as
// "Caps updated. Live within the refresh window.", thresholds rendered "saved", and a feature was
// shown as configured. A gateway 4xx/5xx already answered 422/502 correctly, and budgets and
// connectors propagate failure honestly, so this was an inconsistency rather than a design.
//
// The echo itself is worth keeping: its stated reason is that a fresh clone with no infra still
// works. That reason holds only OFF the product path, which is exactly what the dev escape hatch
// marks (`controlPlaneEchoAllowed`). Both halves are pinned here.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authMock = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

const TENANT_UUID = "3f8c1c2a-0b7e-4d3a-9a1b-77c0d5e2f011";
const originalDevTenant = process.env.TALLY_DEV_TENANT;

/**
 * The control plane is down completely, the org resolve included.
 *
 * This is what an unreachable gateway actually looks like, and an earlier version of this suite
 * quietly excluded it by always answering the resolve. Resolving the tenant is a gateway call like
 * any other, so a stub that answers it exercises a failure mode that cannot occur in isolation and
 * leaves the real one uncovered: on a branch that resolves the tenant ABOVE the route's try, the
 * resolve throws straight out of the handler and Next answers a bare 500, with none of the honest
 * refusal below ever running.
 */
function stubTotalOutage() {
  vi.stubGlobal("fetch", () => Promise.reject(new Error("ECONNREFUSED")));
}

/**
 * The resolve answers and then the write does not: a gateway that goes away mid-request, or one
 * whose control-plane endpoint is failing while the lookup it caches still works.
 */
function stubWritesRefused() {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/tenant/by-clerk-org/")) {
      return Promise.resolve(
        new Response(JSON.stringify({ tenant_id: TENANT_UUID, plan: "free" }), { status: 200 }),
      );
    }
    return Promise.reject(new Error("ECONNREFUSED"));
  });
}

/** Both shapes of outage. The answer to the customer has to be the same for either. */
const OUTAGES: { name: string; stub: () => void }[] = [
  { name: "nothing answers, the org resolve included", stub: stubTotalOutage },
  { name: "the org resolve answers but the write does not", stub: stubWritesRefused },
];

function body(payload: unknown, method = "POST"): Request {
  return new Request("http://test/x", { method, body: JSON.stringify(payload) });
}

const RULE = { id: "gr_x", scope: "a", mode: "warn", maxSteps: 10 };
const THRESHOLDS = { ltvCacGreen: 3, ltvCacYellow: 1, paybackGreen: 6, paybackYellow: 12 };
const MAPPING = { feature: "chatbot", eventName: "paid_conversion" };

afterEach(() => {
  vi.unstubAllGlobals();
  authMock.mockReset();
  if (originalDevTenant === undefined) delete process.env.TALLY_DEV_TENANT;
  else process.env.TALLY_DEV_TENANT = originalDevTenant;
});

describe.each(OUTAGES)("CTO-393: a failed save is reported when $name", ({ stub }) => {
  beforeEach(() => {
    vi.resetModules();
    // Clear the escape hatch so these requests are shaped like a real signed-in customer's.
    delete process.env.TALLY_DEV_TENANT;
    authMock.mockResolvedValue({ orgId: "org_test", orgRole: "org:admin", userId: "user_1" });
    stub();
  });

  const cases: { name: string; call: () => Promise<Response> }[] = [
    {
      name: "POST /api/guardrails",
      call: async () => (await import("./guardrails/route")).POST(body(RULE)),
    },
    {
      name: "POST /api/unit-economics/config",
      call: async () => (await import("./unit-economics/config/route")).POST(body(THRESHOLDS)),
    },
    {
      name: "POST /api/features/value-events",
      call: async () => (await import("./features/value-events/route")).POST(body(MAPPING)),
    },
    {
      // No UI reaches this handler today, so these cases are the only thing exercising its 503.
      // Kept deliberately: it is a public endpoint whichever way the dashboard happens to call it,
      // and a clear-that-never-happened leaves a feature still attributing ROI.
      name: "DELETE /api/features/value-events",
      call: async () =>
        (await import("./features/value-events/route")).DELETE(
          body({ feature: "chatbot" }, "DELETE"),
        ),
    },
  ];

  it.each(cases)("$name answers 503 and never claims the write landed", async ({ call }) => {
    const res = await call();

    expect(res.status).toBe(503);
    const payload = (await res.json()) as { error?: string; persisted?: boolean };
    expect(payload.persisted).toBe(false);
    expect(payload.error).toContain("control plane is unreachable");
  });
});

// These three still pass with the product-path fix reverted, and that is what they are for: they
// pin the echo that must SURVIVE, so tightening the rule above cannot quietly break a fresh clone
// with no infra. The cases that fail on a revert are the product-path ones above.
describe("CTO-393: the dev path still works with no gateway at all", () => {
  beforeEach(() => {
    vi.resetModules();
    // The escape hatch, as a fresh clone and this suite's own config set it. Clerk is never reached.
    process.env.TALLY_DEV_TENANT = "00000000-0000-0000-0000-000000000000";
    stubTotalOutage();
  });

  it("POST /api/guardrails echoes the validated rule, flagged as not persisted", async () => {
    const { POST } = await import("./guardrails/route");

    const res = await POST(body(RULE));

    expect(res.status).toBe(200);
    const payload = (await res.json()) as { changeId: string; persisted: boolean };
    expect(payload.changeId).toBeTypeOf("string");
    // Still false: the echo is a convenience, not a claim that anything was stored.
    expect(payload.persisted).toBe(false);
  });

  it("POST /api/unit-economics/config echoes the validated thresholds", async () => {
    const { POST } = await import("./unit-economics/config/route");

    const res = await POST(body(THRESHOLDS));

    expect(res.status).toBe(200);
    expect((await res.json()).persisted).toBe(false);
  });

  it("POST /api/features/value-events echoes the validated mapping", async () => {
    const { POST } = await import("./features/value-events/route");

    const res = await POST(body(MAPPING));

    expect(res.status).toBe(200);
    expect((await res.json()).persisted).toBe(false);
  });
});
