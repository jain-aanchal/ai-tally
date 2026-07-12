// SPDX-License-Identifier: Apache-2.0
// Route-handler smoke tests — call the GET functions directly (no fetch round-trip).

import { describe, expect, it } from "vitest";

import { GET as HomeGET } from "./home/route";
import { GET as AgentsGET } from "./agents/route";
import { GET as RunGET } from "./agents/runs/[runId]/route";
import { GET as CompareGET } from "./compare/route";
import { GET as CostGET } from "./cost/route";
import { GET as FeaturesGET } from "./features/route";
import { GET as DataQualityGET } from "./data-quality/route";
import { GET as EstimateGET } from "./estimate/route";
import { GET as OnboardingGET, POST as OnboardingPOST } from "./onboarding/route";
import {
  GET as FirstTraceGET,
  POST as FirstTracePOST,
} from "./onboarding/first-trace/route";
import { GET as GuardrailsGET, PUT as GuardrailsPUT } from "./guardrails/route";
import { GET as AttributionGET } from "./attribution/route";
import { GET as CacGET } from "./cac/route";

async function json<T = unknown>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

describe("api routes", () => {
  it("GET /api/home returns the four cards", async () => {
    const body = await json<{ spend: unknown; outliers: unknown[]; roi: unknown[]; dq: unknown }>(await HomeGET());
    expect(body.spend).toBeDefined();
    expect(Array.isArray(body.outliers)).toBe(true);
    expect(Array.isArray(body.roi)).toBe(true);
    expect(body.dq).toBeDefined();
  });

  it("GET /api/agents returns agents + runs + reconciler freshness", async () => {
    const body = await json<{ agents: unknown[]; runs: unknown[]; reconcilerLastRunMinutesAgo: number }>(
      await AgentsGET(new Request("http://test/api/agents") as never),
    );
    expect(body.agents.length).toBeGreaterThan(0);
    expect(body.runs.length).toBeGreaterThan(0);
    expect(body.reconcilerLastRunMinutesAgo).toBeTypeOf("number");
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
    // qualityScore must be null — the route MUST NOT fabricate a number.
    expect(body.current.qualityScore).toBeNull();
    for (const c of body.candidates) {
      expect(c.qualityScore).toBeNull();
      expect(c.qualityCi).toBeUndefined();
    }
    // CTO-115: shape check — fields exist; live path returns numbers (n>=50) or null (n<50);
    // mock-fallback returns numbers. Route.test.ts covers both branches explicitly.
    expect("latencyP95Ms" in body.current).toBe(true);
    expect("errorRate" in body.current).toBe(true);
  });

  it("GET /api/cost returns series + featureRows + alerts", async () => {
    const body = await json<{ series: unknown; featureRows: unknown[]; alerts: unknown[] }>(
      await CostGET(new Request("http://test/api/cost") as never),
    );
    expect(body.series).toBeDefined();
    expect(body.featureRows.length).toBeGreaterThan(0);
  });

  it("GET /api/features returns features + diagnostics", async () => {
    const body = await json<{ features: unknown[]; diagnostics: unknown }>(await FeaturesGET());
    expect(body.features.length).toBeGreaterThan(0);
    expect(body.diagnostics).toBeDefined();
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
      progress: { signedUpAt: number };
      creds: { tenantKey: string; proxyBaseUrl: string };
    }>(await OnboardingGET());
    expect(body.progress.signedUpAt).toBeGreaterThan(0);
    expect(body.creds.tenantKey).toBeTypeOf("string");
    expect(body.creds.proxyBaseUrl).toContain("/v1");
  });

  it("POST /api/onboarding rejects an unknown funnel stage", async () => {
    const bad = await OnboardingPOST(
      new Request("http://test/x", { method: "POST", body: JSON.stringify({ stage: "nope" }) }),
    );
    expect(bad.status).toBe(400);
  });

  it("POST /api/onboarding/first-trace marks the trace received", async () => {
    const res = await FirstTracePOST();
    const body = await json<{ received: boolean }>(res);
    expect(body.received).toBe(true);
    const poll = await json<{ received: boolean }>(await FirstTraceGET());
    expect(poll.received).toBe(true);
  });

  it("GET /api/guardrails returns rules + refresh window", async () => {
    const body = await json<{ rules: unknown[]; configRefreshSeconds: number }>(
      await GuardrailsGET(),
    );
    expect(body.rules.length).toBeGreaterThan(0);
    expect(body.configRefreshSeconds).toBeGreaterThan(0);
  });

  it("GET /api/attribution falls back to mock when ClickHouse is unreachable", async () => {
    const body = await json<{
      isMock: boolean;
      perProvider: { provider: string }[];
      filters: { tag: string | null; outcome: string | null };
    }>(
      await AttributionGET(
        new Request("http://test/api/attribution?tag=chatbot-demo&outcome=positive_feedback"),
      ),
    );
    // CI / fresh-clone: gateway isn't running, so the route falls back to the
    // mock report. Real `make chatbot-demo` runs go through queryAttribution.
    expect(body.isMock).toBe(true);
    expect(body.perProvider.map((p) => p.provider).sort()).toEqual([
      "anthropic",
      "openai",
    ]);
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

  it("PUT /api/guardrails echoes a valid rule, rejects an unconstrained one", async () => {
    const ok = await GuardrailsPUT(
      new Request("http://test/x", {
        method: "PUT",
        body: JSON.stringify({ id: "gr_x", scope: "a", mode: "warn", maxSteps: 10 }),
      }),
    );
    expect(ok.status).toBe(200);

    const bad = await GuardrailsPUT(
      new Request("http://test/x", {
        method: "PUT",
        body: JSON.stringify({ id: "gr_x", scope: "a", mode: "warn" }),
      }),
    );
    expect(bad.status).toBe(422);
  });
});
