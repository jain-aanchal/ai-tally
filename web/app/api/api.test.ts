// SPDX-License-Identifier: Apache-2.0
// Route-handler smoke tests: call the GET functions directly (no fetch round-trip).

import { describe, expect, it } from "vitest";

import { mockSpend } from "@/lib/mock";
import { GET as HomeGET } from "./home/route";
import { GET as AgentsGET } from "./agents/route";
import { GET as RunGET } from "./agents/runs/[runId]/route";
import { GET as CompareGET } from "./compare/route";
import { GET as CostGET } from "./cost/route";
import { GET as FeaturesGET } from "./features/route";
import { GET as DataQualityGET } from "./data-quality/route";
import { GET as EstimateGET } from "./estimate/route";
import { GET as OnboardingGET, POST as OnboardingPOST } from "./onboarding/route";
import { GET as GuardrailsGET, POST as GuardrailsPOST } from "./guardrails/route";
import { GET as AttributionGET } from "./attribution/route";
import { GET as CacGET } from "./cac/route";
import { GET as WasteGET } from "./waste/route";

async function json<T = unknown>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

/**
 * The pinned test tenant (vitest.config.ts) has no telemetry, so every read below answers either
 * `empty` (a stack is up and told us there is nothing) or `unavailable` (no stack to ask). Which one
 * depends on whether the developer running the suite has `make up` going, and CLAUDE.md requires the
 * suite to pass either way, so these smoke tests assert what is true in BOTH: no rows, no figures,
 * and a state that is not `live` and not `sample`.
 *
 * The point of #364 is that those two states are now distinguishable, and each is pinned
 * deterministically over a mocked ClickHouse in `three-states.test.ts`.
 */
function expectNoData(state: string) {
  expect(["empty", "unavailable"]).toContain(state);
}

describe("api routes", () => {
  // #364: these used to assert that rows WERE present, because each route substituted fixtures
  // whenever the live read came back null or empty. That substitution is the bug: the same branch a
  // fresh checkout hit is the branch a real signed-in tenant with no spans hit, and it answered them
  // with another company's numbers. The routes now report the state instead.
  it("GET /api/home returns spend, ROI and data-quality", async () => {
    // CTO-227 dropped the cost-outliers card from Home (replaced by the month-end forecast, which is
    // served by /api/cost/budget), so /api/home no longer carries an `outliers` array.
    const body = await json<{
      spend: unknown;
      roi: unknown[];
      dq: unknown;
      sources: Record<string, string>;
    }>(
      // CTO-226: /api/home reads the time-range window from the request URL; the default (no range
      // param) preserves the historical 30-day view this smoke test asserts.
      await HomeGET(new Request("http://test/api/home")),
    );
    expect(Array.isArray(body.roi)).toBe(true);
    expectNoData(body.sources.spend);
    expectNoData(body.sources.roi);
    // Never mockSpend's $52,400, and never mockRoi's research_agent paying back in 7 days.
    expect(body.spend).not.toEqual(mockSpend);
    expect(body.roi).toEqual([]);
  });

  it("GET /api/agents returns agents + runs + real-or-null reconciler freshness (CTO-169)", async () => {
    const body = await json<{
      agents: unknown[];
      runs: unknown[];
      reconcilerLastRunMinutesAgo: number | null;
      sources: { agents: string; runs: string };
    }>(await AgentsGET(new Request("http://test/api/agents") as never));
    expectNoData(body.sources.agents);
    expectNoData(body.sources.runs);
    expect(body.agents).toEqual([]);
    expect(body.runs).toEqual([]);
    // No reconciler gateway runs in CI, so the route applies honest-null (renders `—`) rather than
    // the old hardcoded constant (23). Real value or null, never a fabricated number.
    expect(body.reconcilerLastRunMinutesAgo === null || typeof body.reconcilerLastRunMinutesAgo === "number").toBe(true);
    expect(body.reconcilerLastRunMinutesAgo).not.toBe(23);
  });

  it("GET /api/agents/runs/:runId returns the run, 404 on miss", async () => {
    const ok = await RunGET(new Request("http://test/x"), { params: Promise.resolve({ runId: "research_run_8af2" }) });
    expect(ok.status).toBe(200);
    const miss = await RunGET(new Request("http://test/x"), { params: Promise.resolve({ runId: "nope" }) });
    expect(miss.status).toBe(404);
  });

  it("GET /api/compare returns a comparison", async () => {
    // CompareGET is async since the live "current model from traffic" wiring;
    // without a live stack the route returns the mock comparison untouched.
    const body = await json<{
      workload: string;
      current: {
        qualityScore: number | null;
        latencyP95Ms: number | null;
        errorRate: number | null;
      };
      candidates: Array<{ qualityScore: number | null; qualityCi?: unknown }>;
    }>(await CompareGET(new Request("http://test/api/compare") as never));
    expect(body.workload).toBeTypeOf("string");
    expect(body.candidates.length).toBeGreaterThan(0);
    // CTO-114: with no eval pass having run (gateway unreachable in tests), every
    // qualityScore must be null; the route MUST NOT fabricate a number.
    expect(body.current.qualityScore).toBeNull();
    for (const c of body.candidates) {
      expect(c.qualityScore).toBeNull();
      expect(c.qualityCi).toBeUndefined();
    }
    // CTO-115: shape check: fields exist; live path returns numbers (n>=50) or null (n<50);
    // mock-fallback returns numbers. Route.test.ts covers both branches explicitly.
    expect("latencyP95Ms" in body.current).toBe(true);
    expect("errorRate" in body.current).toBe(true);
  });

  it("GET /api/cost returns series + featureRows + alerts", async () => {
    const body = await json<{
      series: unknown;
      featureRows: unknown[];
      alerts: unknown[];
      sources: { series: string; featureRows: string; alerts: string };
    }>(await CostGET(new Request("http://test/api/cost") as never));
    expectNoData(body.sources.series);
    expectNoData(body.sources.featureRows);
    expectNoData(body.sources.alerts);
    // No canned feature table and no canned hidden-cost alerts, in either state.
    expect(body.featureRows).toEqual([]);
    expect(body.alerts).toEqual([]);
  });

  it("GET /api/features returns features + diagnostics", async () => {
    const body = await json<{
      features: unknown[];
      diagnostics: unknown;
      sources: { features: string; diagnostics: string };
    }>(await FeaturesGET());
    expectNoData(body.sources.features);
    expect(body.features).toEqual([]);
    // No reconciler run behind the suite, so diagnostics are null rather than the fixture's 180
    // late events and 4.2h median lag.
    expectNoData(body.sources.diagnostics);
    expect(body.diagnostics).toBeNull();
  });

  it("GET /api/data-quality returns a report", async () => {
    const body = await json<{ overall: { attributionRate: number } }>(await DataQualityGET());
    expect(body.overall.attributionRate).toBeGreaterThan(0);
  });

  it("GET /api/estimate returns a projection", async () => {
    const body = await json<{ workload: string; blowUpRisk: number }>(
      await EstimateGET(new Request("http://test/api/estimate") as never),
    );
    expect(body.workload).toBeTypeOf("string");
    expect(body.blowUpRisk).toBeGreaterThanOrEqual(0);
  });

  it("GET /api/onboarding returns progress + creds (no OpenAI key leaked)", async () => {
    const body = await json<{
      progress: Record<string, unknown>;
      creds: { tenantKey: string; proxyBaseUrl: string };
    }>(await OnboardingGET());
    // #358: no signedUpAt. It was the moment the in-process record was built (server boot on the
    // old process-global store), rendered as though it were the tenant's signup.
    expect(body.progress).not.toHaveProperty("signedUpAt");
    expect(body.progress.copiedConfigAt).toBeNull();
    expect(body.creds.tenantKey).toBeTypeOf("string");
    expect(body.creds.proxyBaseUrl).toContain("/v1");
  });

  it("POST /api/onboarding rejects an unknown funnel stage", async () => {
    const bad = await OnboardingPOST(
      new Request("http://test/x", { method: "POST", body: JSON.stringify({ stage: "nope" }) }),
    );
    expect(bad.status).toBe(400);
  });

  // #329: first_trace is reported by the onboarding page off the coverage probe, flagged as an
  // observation. It must be recorded WITHOUT a progress timestamp, because the probe cannot say
  // when the trace arrived and a mirrored timestamp would be read as a measured duration.
  it("POST /api/onboarding records a noticed first_trace without stamping progress", async () => {
    const res = await OnboardingPOST(
      new Request("http://test/x", {
        method: "POST",
        body: JSON.stringify({ stage: "first_trace", noticed: true }),
      }),
    );
    const body = await json<{
      event: { stage: string; noticed?: boolean } | null;
      progress: Record<string, unknown>;
    }>(res);
    expect(body.event?.stage).toBe("first_trace");
    expect(body.event?.noticed).toBe(true);
    expect(body.progress).not.toHaveProperty("firstTraceAt");
  });

  it("GET /api/guardrails returns rules + refresh window", async () => {
    const body = await json<{ rules: unknown[]; configRefreshSeconds: number }>(
      await GuardrailsGET(new Request("http://test/api/guardrails")),
    );
    expect(body.rules.length).toBeGreaterThan(0);
    expect(body.configRefreshSeconds).toBeGreaterThan(0);
  });

  it("GET /api/attribution says the source is unavailable rather than serving the mock report", async () => {
    const body = await json<{
      isMock: boolean;
      state: string;
      perProvider: { provider: string }[];
      totals: { sessions: number };
      filters: { tag: string | null; outcome: string | null };
    }>(
      await AttributionGET(
        new Request("http://test/api/attribution?tag=chatbot-demo&outcome=positive_feedback"),
      ),
    );
    // #364: this used to be answered with the fixture's 5,300 sessions across openai + anthropic.
    // That same branch is what a real tenant with no sessions hit, so it is gone: the state says
    // which of the two no-data answers this is, and no provider row is invented for either.
    expectNoData(body.state);
    expect(body.isMock).toBe(false);
    expect(body.perProvider).toEqual([]);
    // The filters still echo, so the page can caption what it was asked for even with no answer.
    expect(body.filters.tag).toBe("chatbot-demo");
    expect(body.filters.outcome).toBe("positive_feedback");
  });

  it("GET /api/cac falls back to the labelled mock when the gateway is unreachable", async () => {
    // CI / fresh-clone: the gateway isn't running, so queryCacPeriods returns [] and the route
    // serves MOCK_CAC_PERIODS. Real CAC data goes through the live path (isMock=false).
    const body = await json<{
      periods: { periodStart: string; locked: boolean }[];
      economics: Record<string, { arpaMicroUsd: number }>;
      isMock: boolean;
    }>(await CacGET());
    expect(body.isMock).toBe(true);
    expect(body.periods.length).toBeGreaterThan(0);
    // Newest-first ordering.
    expect(body.periods[0].periodStart).toBe("2026-05-01");
    // A period intentionally omits economics (honest-null payback/LTV demo).
    expect(body.economics["2026-03-01"]).toBeUndefined();
    // And at least one period carries economics so the cards render real numbers.
    expect(body.economics["2026-05-01"].arpaMicroUsd).toBeGreaterThan(0);
  });

  it("POST /api/guardrails echoes a valid rule (gateway unreachable), rejects an unconstrained one", async () => {
    // Gateway isn't running in CI / fresh clone, so POST validates and echoes the rule back with a
    // client-supplied change_id rather than blocking on the control plane.
    const ok = await GuardrailsPOST(
      new Request("http://test/x", {
        method: "POST",
        body: JSON.stringify({ id: "gr_x", scope: "a", mode: "warn", maxSteps: 10 }),
      }),
    );
    expect(ok.status).toBe(200);
    const okBody = await json<{ changeId: string }>(ok);
    expect(okBody.changeId).toBeTypeOf("string");

    const bad = await GuardrailsPOST(
      new Request("http://test/x", {
        method: "POST",
        body: JSON.stringify({ id: "gr_x", scope: "a", mode: "warn" }),
      }),
    );
    expect(bad.status).toBe(422);
  });

  it("GET /api/waste returns a WasteReport (findings + byCategory), resilient to empty", async () => {
    // CTO-234: the waste endpoint runs all five detectors and rolls them up with aggregateWaste.
    // Detectors return [] when their data is unavailable, so the report may be empty; assert only the
    // stable WasteReport shape, never a specific finding count.
    const body = await json<{
      findings: unknown[];
      byCategory: Record<string, unknown>;
    }>(await WasteGET(new Request("http://test/api/waste")));
    expect(Array.isArray(body.findings)).toBe(true);
    expect(body.byCategory).toBeTypeOf("object");
    expect(body.byCategory).not.toBeNull();
  });
});
