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
    const body = await json<{ workload: string; current: unknown; candidates: unknown[] }>(CompareGET());
    expect(body.workload).toBeTypeOf("string");
    expect(body.candidates.length).toBeGreaterThan(0);
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
    const body = await json<{ workload: string; blowUpRisk: number }>(EstimateGET());
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
