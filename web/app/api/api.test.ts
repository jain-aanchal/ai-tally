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

  it("GET /api/agents returns agents + runs", async () => {
    const body = await json<{ agents: unknown[]; runs: unknown[] }>(AgentsGET());
    expect(body.agents.length).toBeGreaterThan(0);
    expect(body.runs.length).toBeGreaterThan(0);
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
    const body = await json<{ series: unknown; featureRows: unknown[]; alerts: unknown[] }>(await CostGET());
    expect(body.series).toBeDefined();
    expect(body.featureRows.length).toBeGreaterThan(0);
  });

  it("GET /api/features returns features + diagnostics", async () => {
    const body = await json<{ features: unknown[]; diagnostics: unknown }>(FeaturesGET());
    expect(body.features.length).toBeGreaterThan(0);
    expect(body.diagnostics).toBeDefined();
  });

  it("GET /api/data-quality returns a report", async () => {
    const body = await json<{ overall: { attributionRate: number } }>(DataQualityGET());
    expect(body.overall.attributionRate).toBeGreaterThan(0);
  });

  it("GET /api/estimate returns a projection", async () => {
    const body = await json<{ workload: string; blowUpRisk: number }>(EstimateGET());
    expect(body.workload).toBeTypeOf("string");
    expect(body.blowUpRisk).toBeGreaterThanOrEqual(0);
  });
});
