// SPDX-License-Identifier: Apache-2.0
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  deleteBudget,
  dollarsToMicro,
  microToDollarInput,
  queryBudgets,
  saveBudget,
  scopeLabel,
} from "./budgets";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

const WIRE_ROW = {
  budget_id: "research-agent-2026",
  period: "month",
  amount_micro: 30_000_000_000,
  scope_kind: "feature",
  scope_value: "research-agent",
  starts_on: "2026-01-01",
  ends_on: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("dollarsToMicro", () => {
  it("converts whole dollars exactly", () => {
    expect(dollarsToMicro("30000")).toEqual({ ok: true, micro: 30_000_000_000 });
  });

  it("converts cents exactly, without going through a float", () => {
    // 1250.5 * 1e6 is a float multiplication; these cases are the ones that drift.
    expect(dollarsToMicro("1250.50")).toEqual({ ok: true, micro: 1_250_500_000 });
    expect(dollarsToMicro("0.07")).toEqual({ ok: true, micro: 70_000 });
    expect(dollarsToMicro("0.000001")).toEqual({ ok: true, micro: 1 });
  });

  it("accepts a pasted spreadsheet figure", () => {
    expect(dollarsToMicro("$1,250.50")).toEqual({ ok: true, micro: 1_250_500_000 });
  });

  it("accepts zero, which is a deliberate claim and not 'no budget'", () => {
    expect(dollarsToMicro("0")).toEqual({ ok: true, micro: 0 });
  });

  it("rejects blanks, negatives, text and sub-micro precision", () => {
    for (const bad of ["", "   ", "-5", "abc", "1.2.3", "0.0000001"]) {
      expect(dollarsToMicro(bad).ok).toBe(false);
    }
  });
});

describe("microToDollarInput", () => {
  it("round-trips through dollarsToMicro", () => {
    for (const dollars of ["30000", "1250.5", "0", "0.000001", "999999.999999"]) {
      const parsed = dollarsToMicro(dollars);
      expect(parsed.ok).toBe(true);
      if (!parsed.ok) return;
      expect(microToDollarInput(parsed.micro)).toBe(dollars);
    }
  });
});

describe("scopeLabel", () => {
  it("names the dimension for a scoped budget and says 'whole tenant' otherwise", () => {
    expect(scopeLabel({ scopeKind: "tenant", scopeValue: "" })).toBe("Whole tenant");
    expect(scopeLabel({ scopeKind: "feature", scopeValue: "research-agent" })).toBe(
      "feature: research-agent",
    );
  });
});

describe("queryBudgets", () => {
  it("maps the wire rows and echoes the deployment's allowed periods and scopes", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            tenant_id: "t",
            budgets: [WIRE_ROW],
            configured: true,
            available_periods: ["month", "quarter"],
            available_scope_kinds: ["tenant", "feature", "model", "layer"],
          }),
          { status: 200 },
        ),
    ) as typeof fetch;
    const result = await queryBudgets();
    expect(result.reachable).toBe(true);
    expect(result.configured).toBe(true);
    expect(result.budgets[0]).toMatchObject({
      budgetId: "research-agent-2026",
      amountMicro: 30_000_000_000,
      scopeValue: "research-agent",
      endsOn: null,
    });
    expect(result.periods).toEqual(["month", "quarter"]);
  });

  it("reports no budgets as configured:false and reachable:true, never as an error", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ tenant_id: "t", budgets: [], configured: false }), {
          status: 200,
        }),
    ) as typeof fetch;
    const result = await queryBudgets();
    expect(result).toMatchObject({ configured: false, reachable: true, error: null });
    expect(result.budgets).toEqual([]);
  });

  it("keeps 'unreachable' distinct from 'no budget set'", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    const result = await queryBudgets();
    expect(result.reachable).toBe(false);
    expect(result.error).toBe("ECONNREFUSED");
    // The fallback lists still render a usable form rather than an empty dropdown.
    expect(result.periods).toEqual(["month", "quarter"]);
  });
});

describe("saveBudget", () => {
  it("posts integer micro-USD and a null ends_on for an open-ended budget", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify({ budget: WIRE_ROW }), { status: 200 }),
    );
    globalThis.fetch = fetchMock;
    const result = await saveBudget({
      budgetId: "research-agent-2026",
      period: "month",
      amountMicro: 30_000_000_000,
      scopeKind: "feature",
      scopeValue: "research-agent",
      startsOn: "2026-01-01",
      endsOn: null,
    });
    expect(result.ok).toBe(true);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.amount_micro).toBe(30_000_000_000);
    expect(Number.isInteger(body.amount_micro)).toBe(true);
    expect(body.ends_on).toBeNull();
  });

  it("surfaces a 409 overlap message verbatim and carries the colliding budget_id", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            detail: {
              message:
                "a budget already covers this scope and period over an overlapping date range: " +
                "research-agent-2026",
              conflicting_budget_id: "research-agent-2026",
            },
          }),
          { status: 409 },
        ),
    ) as typeof fetch;
    const result = await saveBudget({
      budgetId: "another",
      period: "month",
      amountMicro: 1,
      scopeKind: "feature",
      scopeValue: "research-agent",
      startsOn: "2026-01-01",
      endsOn: null,
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toContain("research-agent-2026");
    expect(result.conflictingBudgetId).toBe("research-agent-2026");
  });

  it("surfaces a 422 validation string verbatim", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "a feature-scoped budget requires a scope_value" }), {
          status: 422,
        }),
    ) as typeof fetch;
    const result = await saveBudget({
      budgetId: "x",
      period: "month",
      amountMicro: 1,
      scopeKind: "feature",
      scopeValue: "",
      startsOn: "2026-01-01",
      endsOn: null,
    });
    expect(result).toEqual({
      ok: false,
      error: "a feature-scoped budget requires a scope_value",
      conflictingBudgetId: null,
    });
  });

  it("falls back to the status code only when the gateway said nothing useful", async () => {
    globalThis.fetch = vi.fn(async () => new Response("boom", { status: 500 })) as typeof fetch;
    const result = await saveBudget({
      budgetId: "x",
      period: "month",
      amountMicro: 1,
      scopeKind: "tenant",
      scopeValue: "",
      startsOn: "2026-01-01",
      endsOn: null,
    });
    expect(result).toMatchObject({ ok: false, error: "gateway HTTP 500" });
  });
});

describe("deleteBudget", () => {
  it("reports removed:false for an already-absent budget rather than failing", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(JSON.stringify({ removed: false }), { status: 200 }),
    ) as typeof fetch;
    expect(await deleteBudget("gone")).toEqual({ ok: true, removed: false });
  });

  it("passes the budget_id as a query parameter", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify({ removed: true }), { status: 200 }),
    );
    globalThis.fetch = fetchMock;
    await deleteBudget("research agent/2026");
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "budget_id=research%20agent%2F2026",
    );
  });
});
