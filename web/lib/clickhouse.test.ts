// SPDX-License-Identifier: Apache-2.0
// CTO-115: queryCurrentModel latency/error suppression.
//
// We exercise the SUT by stubbing the @clickhouse/client query() method through vi.mock —
// the function under test is the small adapter that converts rows into the typed return value
// (and applies the n < 50 suppression rule), so we don't need a real ClickHouse to test it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type RowShape = Record<string, unknown>;

const queryMock = vi.fn<(args: unknown) => Promise<{ json: () => Promise<RowShape[]> }>>();

vi.mock("@clickhouse/client", () => ({
  createClient: () => ({
    query: (args: unknown) => queryMock(args),
  }),
}));

async function freshSut() {
  // Reset the module cache so the `_client` singleton in clickhouse.ts is recreated and picks
  // up the new mock state per test.
  vi.resetModules();
  return await import("./clickhouse");
}

function respond(row: RowShape | null) {
  queryMock.mockResolvedValueOnce({
    json: async () => (row ? [row] : []),
  });
}

function respondRows(rowsData: RowShape[]) {
  queryMock.mockResolvedValueOnce({
    json: async () => rowsData,
  });
}

beforeEach(() => {
  queryMock.mockReset();
});

describe("queryCurrentModel — latency/error suppression (CTO-115)", () => {
  it("returns null when ClickHouse has no rows (route falls back to mock)", async () => {
    const { queryCurrentModel } = await freshSut();
    respond(null);
    const out = await queryCurrentModel();
    expect(out).toBeNull();
  });

  it("suppresses latencyP95Ms and errorRate to null when sampleCount < 50", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "claude-sonnet-4.5",
      provider: "anthropic",
      cost7d: "1.40",
      p95Ms: 2400,
      errRate: 0.02,
      sampleCount: 12,
    });
    const out = await queryCurrentModel();
    expect(out).not.toBeNull();
    expect(out!.model).toBe("claude-sonnet-4.5");
    expect(out!.sampleCount).toBe(12);
    expect(out!.latencyP95Ms).toBeNull();
    expect(out!.errorRate).toBeNull();
    // Cost projection still runs — it's a sum, not a quantile.
    expect(out!.monthlyCostMicroUsd).toBe(Math.round((1_400_000 * 30) / 7));
  });

  it("returns real numbers when sampleCount >= 50", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "claude-sonnet-4.5",
      provider: "anthropic",
      cost7d: "1.40",
      p95Ms: 2400.7,
      errRate: 0.004,
      sampleCount: 500,
    });
    const out = await queryCurrentModel();
    expect(out).not.toBeNull();
    expect(out!.latencyP95Ms).toBe(2401);
    expect(out!.errorRate).toBeCloseTo(0.004, 6);
    expect(out!.sampleCount).toBe(500);
  });

  it("handles ClickHouse string-encoded numerics", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "gpt-5-mini",
      provider: "openai",
      cost7d: "0.50",
      p95Ms: "1800.0",
      errRate: "0.01",
      sampleCount: "75",
    });
    const out = await queryCurrentModel();
    expect(out!.latencyP95Ms).toBe(1800);
    expect(out!.errorRate).toBeCloseTo(0.01, 6);
    expect(out!.sampleCount).toBe(75);
  });
});

// CTO-171: guard the Compare candidate list against silently shipping a retired model id.
//
// The gateway discovers its live provider lineup at boot but does not expose it over HTTP yet
// (no `/v1/models` route — that's the follow-up), so DEFAULT_CANDIDATES stays hardcoded. This
// test is the safety net: it mirrors the current, catalog-priced ids from the SDK's seed_catalog()
// (sdk/python/src/tally/pricing.py) and asserts every candidate is one of them. A retired id like
// `gpt-4o-mini` — which is still *priced* in the catalog for backward compat but is NOT a model we
// want the switcher to surface — must not appear. If seed_catalog() gains/loses a current model,
// update KNOWN_CURRENT_CANDIDATES here in the same change.
// CTO-146: per-rule trip counts sourced from guardrail-verdict spans.
//
// queryGuardrailActivity aggregates SpanAttributes['gen_ai.guardrail.{rule_id}.verdict'] over 7d:
//   runsThisWeek           = every verdict row for the rule (it was evaluated)
//   wouldHaveFiredThisWeek = verdict ∈ {enforced, shadow_observed}
// We stub the ClickHouse rows the ARRAY JOIN would produce and assert the mapping.
describe("queryGuardrailActivity — verdict-span trip counts (CTO-146)", () => {
  it("maps verdict rows to runs/wouldFire counts per rule", async () => {
    const { queryGuardrailActivity } = await freshSut();
    respondRows([
      { ruleId: "gr_research_cost", runs: "8680", wouldFire: "312" },
      { ruleId: "gr_support_steps", runs: "58100", wouldFire: "1240" },
    ]);
    const activity = await queryGuardrailActivity();
    expect(activity).not.toBeNull();
    expect(activity!.get("gr_research_cost")).toEqual({
      runsThisWeek: 8680,
      wouldHaveFiredThisWeek: 312,
    });
    expect(activity!.get("gr_support_steps")).toEqual({
      runsThisWeek: 58100,
      wouldHaveFiredThisWeek: 1240,
    });
  });

  it("counts a shadow rule's would-fires without counting them as enforcement", async () => {
    // A rule fully in shadow: every fire is shadow_observed, so wouldFire tracks would-have-fired,
    // and it never enforces. The query does not distinguish enforced vs shadow in wouldFire — both
    // count toward 'would have fired' — which is exactly the graduation signal.
    const { queryGuardrailActivity } = await freshSut();
    respondRows([{ ruleId: "gr_shadow", runs: "1000", wouldFire: "47" }]);
    const activity = await queryGuardrailActivity();
    const a = activity!.get("gr_shadow")!;
    expect(a.runsThisWeek).toBe(1000);
    expect(a.wouldHaveFiredThisWeek).toBe(47);
    expect(a.wouldHaveFiredThisWeek).toBeLessThan(a.runsThisWeek);
  });

  it("is honest about a rule with no telemetry — absent from the map (renders as —)", async () => {
    const { queryGuardrailActivity } = await freshSut();
    respondRows([{ ruleId: "gr_active", runs: "500", wouldFire: "5" }]);
    const activity = await queryGuardrailActivity();
    // A rule the query never saw is simply not in the map — the caller leaves its counts at 0,
    // which the UI renders as `—`, never a fabricated number.
    expect(activity!.has("gr_never_ran")).toBe(false);
    expect(activity!.get("gr_active")).toEqual({ runsThisWeek: 500, wouldHaveFiredThisWeek: 5 });
  });

  it("drops rows with an empty extracted rule id", async () => {
    const { queryGuardrailActivity } = await freshSut();
    respondRows([
      { ruleId: "", runs: "10", wouldFire: "1" },
      { ruleId: "gr_ok", runs: "20", wouldFire: "2" },
    ]);
    const activity = await queryGuardrailActivity();
    expect(activity!.has("")).toBe(false);
    expect(activity!.size).toBe(1);
    expect(activity!.get("gr_ok")).toEqual({ runsThisWeek: 20, wouldHaveFiredThisWeek: 2 });
  });
});

describe("DEFAULT_CANDIDATES guard (CTO-171)", () => {
  // Current, non-retired models we're willing to surface in Compare, keyed "provider/model".
  // Kept in sync by hand with seed_catalog() until discovery is exposed over HTTP.
  const KNOWN_CURRENT_CANDIDATES = new Set([
    "anthropic/claude-haiku-4-5",
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-opus-4-8",
    "openai/gpt-5-mini",
    "openai/gpt-5",
    "google/gemini-3-flash",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
  ]);

  // Priced-but-retired ids that must never leak back into the switcher.
  const RETIRED_IDS = new Set(["openai/gpt-4o-mini"]);

  it("lists only current, catalog-priced models", async () => {
    const { DEFAULT_CANDIDATES } = await freshSut();
    expect(DEFAULT_CANDIDATES.length).toBeGreaterThan(0);
    for (const c of DEFAULT_CANDIDATES) {
      const id = `${c.provider}/${c.model}`;
      expect(KNOWN_CURRENT_CANDIDATES.has(id), `${id} is not a current catalog model`).toBe(true);
    }
  });

  it("does not include the retired gpt-4o-mini", async () => {
    const { DEFAULT_CANDIDATES } = await freshSut();
    for (const c of DEFAULT_CANDIDATES) {
      const id = `${c.provider}/${c.model}`;
      expect(RETIRED_IDS.has(id), `${id} is retired and must not be a candidate`).toBe(false);
    }
  });
});

// CTO-169: reconciler "last run" freshness is derived from the real reconciliation_runs source
// (gateway GET /v1/tenant/reconciliation/status), not a hardcoded constant. Honest-null when the
// reconciler has never run or the gateway is unavailable — the caller renders `—`.
describe("queryReconcilerLastRun (CTO-169)", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  function stubFetch(impl: () => Promise<Response>) {
    globalThis.fetch = vi.fn(impl) as unknown as typeof fetch;
  }

  it("derives minutes-ago from the latest run's finished_at", async () => {
    const finished = new Date(Date.now() - 12 * 60_000).toISOString(); // 12 minutes ago
    stubFetch(async () => new Response(
      JSON.stringify({ run: { events_late: 3, lag_seconds_median: 120, finished_at: finished } }),
      { status: 200 },
    ));
    const { queryReconcilerLastRun } = await freshSut();
    const out = await queryReconcilerLastRun();
    expect(out).toBe(12);
  });

  it("returns null (honest-null → `—`) when no reconciler run exists yet", async () => {
    stubFetch(async () => new Response(JSON.stringify({ run: null }), { status: 200 }));
    const { queryReconcilerLastRun } = await freshSut();
    expect(await queryReconcilerLastRun()).toBeNull();
  });

  it("returns null when the gateway is unreachable", async () => {
    stubFetch(async () => {
      throw new Error("ECONNREFUSED");
    });
    const { queryReconcilerLastRun } = await freshSut();
    expect(await queryReconcilerLastRun()).toBeNull();
  });

  it("returns null on a non-2xx gateway response", async () => {
    stubFetch(async () => new Response("nope", { status: 503 }));
    const { queryReconcilerLastRun } = await freshSut();
    expect(await queryReconcilerLastRun()).toBeNull();
  });
});
