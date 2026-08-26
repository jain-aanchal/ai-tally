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

describe("queryDistinctBusinessEventNames (CTO-140)", () => {
  it("maps observed events into {name, count}, most-frequent first as returned", async () => {
    const { queryDistinctBusinessEventNames } = await freshSut();
    queryMock.mockResolvedValueOnce({
      json: async () => [
        { name: "subscription_created", n: "120" },
        { name: "paid_conversion", n: "42" },
      ],
    });
    const out = await queryDistinctBusinessEventNames();
    expect(out).toEqual([
      { name: "subscription_created", count: 120 },
      { name: "paid_conversion", count: 42 },
    ]);
  });

  it("returns an empty array (not null) when no events exist", async () => {
    const { queryDistinctBusinessEventNames } = await freshSut();
    respond(null);
    expect(await queryDistinctBusinessEventNames()).toEqual([]);
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

// --- Per-customer cost (CTO-187, D1) ------------------------------------------------------------
//
// The test that matters here is RECONCILIATION: per-account direct cost plus the unattributed
// bucket must equal the tenant's direct total for the window. If it does not, this tab contradicts
// /cost and loses trust immediately (docs/cost-per-customer-plan.md, "Risks carried from the
// scope"). The rest guard the three ways that identity is easy to break by accident: folding
// compute and egress in, counting distinct users with a raw count instead of merging the uniq
// state, and drifting off the calendar-aligned window the other surfaces use.

const ACCT_A = "a".repeat(64);
const ACCT_B = "b".repeat(64);

// One fixture, stated in plain USD, feeding both the query stubs and the expected total below, so
// the assertion cannot drift away from the input it is checking.
const FIXTURE = [
  { account: ACCT_A, layer: "llm", usd: 10.5 },
  { account: ACCT_A, layer: "tools", usd: 2.25 },
  { account: ACCT_B, layer: "llm", usd: 4 },
  { account: ACCT_B, layer: "vector", usd: 1 },
  { account: "", layer: "llm", usd: 3.25 },
  { account: "", layer: "embeddings", usd: 0.5 },
];
const TENANT_DIRECT_MICRO = Math.round(FIXTURE.reduce((s, r) => s + r.usd, 0) * 1_000_000);

function respondAccountCosts() {
  respondRows([
    { account: ACCT_A, spans: "100", users: "7" },
    { account: ACCT_B, spans: "50", users: "3" },
    { account: "", spans: "25", users: "2" },
  ]);
  respondRows(FIXTURE.map((r) => ({ account: r.account, layer: r.layer, cost: String(r.usd) })));
}

describe("queryAccountCosts — reconciliation (CTO-187)", () => {
  it("per-account plus unattributed equals the tenant direct total", async () => {
    const { queryAccountCosts } = await freshSut();
    respondAccountCosts();
    const out = await queryAccountCosts();
    expect(out).not.toBeNull();

    const sumAccounts = out!.accounts.reduce((s, a) => s + a.directCostMicroUsd, 0);
    const reconciled = sumAccounts + out!.unattributed.directCostMicroUsd;

    // The identity, two ways: against the total the function reports, and against the fixture
    // totalled independently of the code under test.
    expect(reconciled).toBe(out!.totalDirectMicroUsd);
    expect(reconciled).toBe(TENANT_DIRECT_MICRO);
  });

  it("keeps the unattributed bucket out of the ranked accounts and flags it", async () => {
    const { queryAccountCosts } = await freshSut();
    respondAccountCosts();
    const out = await queryAccountCosts();
    expect(out!.accounts.map((a) => a.accountIdHash)).toEqual([ACCT_A, ACCT_B]);
    expect(out!.accounts.every((a) => !a.unattributed)).toBe(true);
    expect(out!.unattributed.unattributed).toBe(true);
    expect(out!.unattributed.accountIdHash).toBe("");
    expect(out!.unattributed.directCostMicroUsd).toBe(3_750_000); // 3.25 + 0.50
  });

  it("ranks accounts by direct cost, most expensive first", async () => {
    const { queryAccountCosts } = await freshSut();
    respondAccountCosts();
    const out = await queryAccountCosts();
    expect(out!.accounts[0].directCostMicroUsd).toBe(12_750_000); // 10.50 + 2.25
    expect(out!.accounts[1].directCostMicroUsd).toBe(5_000_000); // 4.00 + 1.00
  });

  it("returns a zeroed unattributed bucket when the window holds no unattributed spans", async () => {
    const { queryAccountCosts } = await freshSut();
    // A tenant that has instrumented every span. The bucket must still be present so the page can
    // state the unattributed share as zero rather than omitting the sentence entirely.
    respondRows([{ account: ACCT_A, spans: "100", users: "7" }]);
    respondRows([{ account: ACCT_A, layer: "llm", cost: "10.5" }]);
    const out = await queryAccountCosts();
    expect(out!.unattributed.unattributed).toBe(true);
    expect(out!.unattributed.directCostMicroUsd).toBe(0);
    expect(out!.unattributed.spanCount).toBe(0);
    expect(out!.totalDirectMicroUsd).toBe(10_500_000);
  });

  it("carries distinct users and span count through per account", async () => {
    const { queryAccountCosts } = await freshSut();
    respondAccountCosts();
    const out = await queryAccountCosts();
    expect(out!.accounts[0].distinctUsers).toBe(7);
    expect(out!.accounts[0].spanCount).toBe(100);
    expect(out!.windowDays).toBe(30);
  });
});

describe("queryAccountCosts — SQL contract (CTO-187)", () => {
  async function sqlOf(): Promise<string[]> {
    const { queryAccountCosts } = await freshSut();
    respondAccountCosts();
    await queryAccountCosts();
    return queryMock.mock.calls.map((c) => (c[0] as { query: string }).query);
  }

  it("reads the account rollup, never a per-account scan of otel_spans", async () => {
    for (const q of await sqlOf()) {
      expect(q).toContain("FROM daily_account_rollup");
      expect(q).not.toContain("otel_spans");
    }
  });

  it("excludes compute and egress from direct cost (CTO-189 surfaces them separately)", async () => {
    for (const q of await sqlOf()) {
      expect(q).toContain("GenAiOperation NOT IN ('compute', 'egress')");
    }
  });

  it("merges the uniq aggregate state rather than counting rows", async () => {
    const [totals] = await sqlOf();
    expect(totals).toContain("uniqMerge(UserCountState)");
    expect(totals).not.toContain("uniqExact(UserIdHash)");
  });

  it("uses the calendar-aligned window Home and /cost use, not a rolling one", async () => {
    for (const q of await sqlOf()) {
      expect(q).toContain("Day >= toDate(now()) - INTERVAL 29 DAY");
      expect(q).not.toContain("now() - INTERVAL 30 DAY");
    }
  });

  it("normalises the NUL-padded FixedString so the unattributed bucket is a real empty string", async () => {
    for (const q of await sqlOf()) {
      expect(q).toContain("if(empty(AccountIdHash), '', toString(AccountIdHash))");
    }
  });
});

describe("queryExcludedInfraCost: what the per-account table leaves out (CTO-189)", () => {
  async function sqlOf(): Promise<string> {
    const { queryExcludedInfraCost } = await freshSut();
    respondRows([]);
    await queryExcludedInfraCost();
    return (queryMock.mock.calls[0][0] as { query: string }).query;
  }

  it("sums compute and egress separately and totals them", async () => {
    const { queryExcludedInfraCost } = await freshSut();
    respondRows([
      { layer: "compute", cost: "40.5" },
      { layer: "egress", cost: "6.5" },
    ]);
    const out = await queryExcludedInfraCost();
    expect(out).toEqual({
      windowDays: 30,
      computeMicroUsd: 40_500_000,
      egressMicroUsd: 6_500_000,
      totalMicroUsd: 47_000_000,
    });
  });

  it("reports a real zero when the tenant has no infrastructure spend", async () => {
    const { queryExcludedInfraCost } = await freshSut();
    respondRows([]);
    const out = await queryExcludedInfraCost();
    expect(out!.totalMicroUsd).toBe(0);
  });

  it("returns null when ClickHouse is unreachable, so the page cannot read failure as zero", async () => {
    const { queryExcludedInfraCost } = await freshSut();
    queryMock.mockRejectedValueOnce(new Error("connect ECONNREFUSED"));
    expect(await queryExcludedInfraCost()).toBeNull();
  });

  it("ignores a direct layer that somehow reaches it rather than folding it into the excluded total", async () => {
    // Belt and braces against the SQL filter regressing: an llm row here would otherwise inflate
    // the banner and understate the coverage of the table beneath it.
    const { queryExcludedInfraCost } = await freshSut();
    respondRows([
      { layer: "llm", cost: "100.0" },
      { layer: "compute", cost: "1.0" },
    ]);
    const out = await queryExcludedInfraCost();
    expect(out!.totalMicroUsd).toBe(1_000_000);
  });

  it("reads exactly the complement of the direct-cost filter, so the two sum to the tenant total", async () => {
    expect(await sqlOf()).toContain("NOT (GenAiOperation NOT IN ('compute', 'egress'))");
  });

  it("uses the same rollup and the same calendar-aligned window as the table", async () => {
    const q = await sqlOf();
    expect(q).toContain("FROM daily_account_rollup");
    expect(q).toContain("Day >= toDate(now()) - INTERVAL 29 DAY");
    expect(q).not.toContain("otel_spans");
  });

  it("is scoped to one tenant", async () => {
    expect(await sqlOf()).toContain("TenantId = {tenant:String}");
  });
});

describe("queryAccountDetail (CTO-187)", () => {
  const WINDOW_START = "2026-07-28";

  function respondDetail(opts: {
    seen: string;
    spans: string;
    users: string;
    trend?: RowShape[];
    runs?: RowShape[];
  }) {
    respondRows([
      { seen: opts.seen, spans: opts.spans, users: opts.users, windowStart: WINDOW_START },
    ]);
    respondRows([
      { layer: "llm", cost: "10.5" },
      { layer: "tools", cost: "2.25" },
    ]);
    respondRows([{ feature: "research_agent", cost: "9", spans: "80" }]);
    respondRows(opts.trend ?? [{ day: "2026-08-01", cost: "12.75" }]);
    respondRows(
      opts.runs ?? [
        { runId: "trace-1", agent: "aider", cost: "8", steps: "40", maxStatus: "0" },
      ],
    );
  }

  it("returns null for an account the tenant has never seen", async () => {
    const { queryAccountDetail } = await freshSut();
    // An aggregate with no GROUP BY always returns a row, so `seen` is what separates "unknown
    // account" from "account that cost nothing". Honest-null beats inventing a customer at $0.
    respondRows([{ seen: "0", spans: "0", users: "0", windowStart: WINDOW_START }]);
    expect(await queryAccountDetail(ACCT_A)).toBeNull();
  });

  it("agrees with the list row for the same account", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail({ seen: "12", spans: "100", users: "7" });
    const out = await queryAccountDetail(ACCT_A);
    expect(out!.accountIdHash).toBe(ACCT_A);
    expect(out!.unattributed).toBe(false);
    expect(out!.directCostMicroUsd).toBe(12_750_000);
    expect(out!.distinctUsers).toBe(7);
    expect(out!.spanCount).toBe(100);
  });

  it("flags the unattributed bucket when asked for ''", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail({ seen: "12", spans: "100", users: "7" });
    const out = await queryAccountDetail("");
    expect(out!.unattributed).toBe(true);
  });

  it("anchors the trend on ClickHouse's window start, not the Node clock", async () => {
    const { queryAccountDetail } = await freshSut();
    // The oldest day in the window must have a slot to land in. Anchoring the day list on the Node
    // clock instead shifts it by a day whenever the two timezones straddle midnight, silently
    // dropping this point from the chart while it still counts toward the total above it.
    respondDetail({
      seen: "12",
      spans: "100",
      users: "7",
      trend: [{ day: WINDOW_START, cost: "12.75" }],
    });
    const out = await queryAccountDetail(ACCT_A);
    expect(out!.trend).toHaveLength(30);
    expect(out!.trend[0].date).toBe(WINDOW_START);
    expect(out!.trend[29].date).toBe("2026-08-26");
    expect(out!.trend[0].directCostMicroUsd).toBe(12_750_000);
    // Filled days are real zeroes for this account, not missing data.
    expect(out!.trend[1].directCostMicroUsd).toBe(0);
    // And the trend must add up to the headline it sits under.
    expect(out!.trend.reduce((s, p) => s + p.directCostMicroUsd, 0)).toBe(out!.directCostMicroUsd);
  });

  it("caps top features and leaves untagged traffic out of the list", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail({ seen: "12", spans: "100", users: "7" });
    const out = await queryAccountDetail(ACCT_A);
    expect(out!.topFeatures).toEqual([
      { feature: "research_agent", directCostMicroUsd: 9_000_000, spanCount: 80 },
    ]);
    const featureSql = (queryMock.mock.calls[2][0] as { query: string }).query;
    expect(featureSql).toContain("LIMIT 5");
    expect(featureSql).toContain("FeatureTag != ''");
  });
});

describe("queryAccountDetail heaviest runs (CTO-190)", () => {
  const WINDOW_START = "2026-07-28";

  function respondDetail(runs?: RowShape[]) {
    respondRows([{ seen: "12", spans: "100", users: "7", windowStart: WINDOW_START }]);
    respondRows([{ layer: "llm", cost: "10.5" }]);
    respondRows([]);
    respondRows([]);
    respondRows(
      runs ?? [
        { runId: "trace-1", agent: "aider", cost: "8", steps: "40", maxStatus: "0" },
        { runId: "trace-2", agent: "", cost: "3", steps: "9", maxStatus: "2" },
      ],
    );
  }

  /** The runs read is the 5th and last query the detail path issues. */
  function runSql(): string {
    return (queryMock.mock.calls[4][0] as { query: string }).query;
  }

  it("returns the account's share of each run, ranked, capped, with the outcome", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail();
    const out = await queryAccountDetail(ACCT_A);
    expect(out!.topRuns).toEqual([
      {
        runId: "trace-1",
        agent: "aider",
        accountCostMicroUsd: 8_000_000,
        steps: 40,
        outcome: "success",
      },
      {
        // An untagged ServiceName reads as "untagged" rather than as an empty agent name, which is
        // what /agents does for the same row.
        runId: "trace-2",
        agent: "untagged",
        accountCostMicroUsd: 3_000_000,
        steps: 9,
        outcome: "failed",
      },
    ]);
    expect(runSql()).toContain("LIMIT 8");
  });

  it("groups this account's spans, never whole traces that merely touch the account", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail();
    await queryAccountDetail(ACCT_A);
    const sql = runSql();
    // The account predicate has to sit in the WHERE, not in a HAVING or a subquery that widens
    // back out to the trace: a run serving several customers would otherwise report its full cost
    // against every one of them and the same money would land on several bills.
    expect(sql).toContain("AccountIdHash = {account:String}");
    expect(sql).toContain("GROUP BY TraceId");
  });

  it("uses the same calendar-aligned window as the totals it sits under", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail();
    await queryAccountDetail(ACCT_A);
    // A rolling `now() - INTERVAL 30 DAY` here would let a run appear in the list that the account
    // total above it does not count.
    expect(runSql()).toContain("Timestamp >= toDate(now()) - INTERVAL 29 DAY");
    expect(runSql()).not.toContain("now() - INTERVAL 30 DAY");
    expect(runSql()).toContain("GenAiOperation NOT IN ('compute', 'egress')");
  });

  it("reads run grain from otel_spans, which is the only table carrying a trace id", async () => {
    const { queryAccountDetail } = await freshSut();
    respondDetail();
    await queryAccountDetail(ACCT_A);
    expect(runSql()).toContain("otel_spans");
    // Every other read on this path stays on the rollup.
    for (const i of [0, 1, 2, 3]) {
      expect((queryMock.mock.calls[i][0] as { query: string }).query).toContain(
        "daily_account_rollup",
      );
    }
  });
});

describe("queryAccountDetailResult (CTO-190)", () => {
  const WINDOW_START = "2026-07-28";

  it("separates an unknown account from an unreachable store", async () => {
    const { queryAccountDetailResult } = await freshSut();
    // Reachable, no rollup rows: an account we have never seen. Not an error, and not a 404.
    respondRows([{ seen: "0", spans: "0", users: "0", windowStart: WINDOW_START }]);
    expect(await queryAccountDetailResult(ACCT_A)).toEqual({ state: "unknown" });

    // The store itself refusing. queryAccountDetail collapses this into the same null as the case
    // above, which would have the page say "no spend recorded" about an account it cannot read.
    queryMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    expect(await queryAccountDetailResult(ACCT_A)).toEqual({ state: "unreachable" });
  });

  it("returns the detail when there is one", async () => {
    const { queryAccountDetailResult } = await freshSut();
    respondRows([{ seen: "12", spans: "100", users: "7", windowStart: WINDOW_START }]);
    respondRows([{ layer: "llm", cost: "10.5" }]);
    respondRows([]);
    respondRows([]);
    respondRows([]);
    const result = await queryAccountDetailResult(ACCT_A);
    expect(result.state).toBe("ok");
    expect(result.state === "ok" && result.detail.accountIdHash).toBe(ACCT_A);
  });
});

// CTO-184: account stitching + the multi-account refusal.
//
// Same shape as the tests above: we stub @clickhouse/client and exercise the adapter that turns
// rows into the typed result. The queries fire in a fixed order (coverage, direct accounts,
// conflicting users, then, only when there are any, their withheld cost) so the stubs queue in
// that order.
describe("queryAccountStitching (CTO-184)", () => {
  it("counts direct and stitched accounts separately so the UI can show confidence", async () => {
    const { queryAccountStitching } = await freshSut();
    respondRows([{ stitched_accounts: "42", stitched_users: "1340" }]);
    respondRows([{ direct_accounts: "18" }]);
    respondRows([]); // no conflicts
    const out = await queryAccountStitching();
    expect(out).toEqual({
      directAccounts: 18,
      stitchedAccounts: 42,
      stitchedUsers: 1340,
      conflicts: [],
    });
  });

  it("skips the cost query entirely when nothing is ambiguous", async () => {
    const { queryAccountStitching } = await freshSut();
    respondRows([{ stitched_accounts: "3", stitched_users: "9" }]);
    respondRows([{ direct_accounts: "0" }]);
    respondRows([]);
    await queryAccountStitching();
    expect(queryMock).toHaveBeenCalledTimes(3); // no fourth query over an empty user list
  });

  it("surfaces a multi-account user as a finding, with both accounts and the withheld spend", async () => {
    const { queryAccountStitching } = await freshSut();
    respondRows([{ stitched_accounts: "5", stitched_users: "80" }]);
    respondRows([{ direct_accounts: "2" }]);
    respondRows([{ person_hash: "u_alice", accounts: ["acct_gadgets", "acct_widgets"] }]);
    respondRows([{ user_hash: "u_alice", cost: "4.82", spans: "312" }]);
    const out = await queryAccountStitching();
    // Nothing is attributed for this user; the conflict is reported instead of guessed at.
    expect(out!.conflicts).toEqual([
      {
        userIdHash: "u_alice",
        accounts: ["acct_gadgets", "acct_widgets"],
        withheldMicroUsd: 4_820_000,
        spans30d: 312,
      },
    ]);
  });

  it("reports a conflict even when the user has no spend to withhold", async () => {
    const { queryAccountStitching } = await freshSut();
    respondRows([{ stitched_accounts: "5", stitched_users: "80" }]);
    respondRows([{ direct_accounts: "2" }]);
    respondRows([{ person_hash: "u_bob", accounts: ["acct_a", "acct_b"] }]);
    respondRows([]); // no spans matched, but the ambiguity is still a finding
    const out = await queryAccountStitching();
    expect(out!.conflicts).toHaveLength(1);
    expect(out!.conflicts[0].withheldMicroUsd).toBe(0);
    expect(out!.conflicts[0].spans30d).toBe(0);
  });

  it("trims FixedString NUL padding off hashes so they still join", async () => {
    // A FixedString(64) read back shorter than 64 bytes comes over NUL-padded.
    const PAD = "\u0000\u0000";
    const { queryAccountStitching } = await freshSut();
    respondRows([{ stitched_accounts: "1", stitched_users: "1" }]);
    respondRows([{ direct_accounts: "0" }]);
    respondRows([{ person_hash: `u_pad${PAD}`, accounts: [`acct_a${PAD}`, "acct_b"] }]);
    respondRows([{ user_hash: `u_pad${PAD}`, cost: "1.00", spans: "1" }]);
    const out = await queryAccountStitching();
    expect(out!.conflicts[0].userIdHash).toBe("u_pad");
    expect(out!.conflicts[0].accounts).toEqual(["acct_a", "acct_b"]);
    expect(out!.conflicts[0].withheldMicroUsd).toBe(1_000_000);
  });

  it("returns null when ClickHouse is unreachable, so the route falls back to mock", async () => {
    const { queryAccountStitching } = await freshSut();
    queryMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    expect(await queryAccountStitching()).toBeNull();
  });
});
