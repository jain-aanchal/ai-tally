// SPDX-License-Identifier: Apache-2.0
// Route tests for /api/estimate: the sample gate (CTO-298), the body-driven what-if and the
// honest-null floor (CTO-128).
//
// CTO-298 is the reason most of the assertions below are about what is ABSENT. This route answered
// every caller with lib/estimate's fixture: a $19,100/mo baseline, a 42% blow-up risk, three
// invented cost drivers and a pull request (jain-aanchal/ai-tally#1284) that exists in no
// repository. It is unlinked from the nav, which protects nobody, because middleware.ts treats it
// as an ordinary signed-in route. So each case here pins that a real tenant sees none of it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/clickhouse", () => ({
  queryReplayCandidates: vi.fn().mockResolvedValue(null),
  queryReplayEstimate: vi.fn().mockResolvedValue(null),
  // CTO-169: reconciler last-run is read from the real source; default to null (honest-null) here.
  queryReconcilerLastRun: vi.fn().mockResolvedValue(null),
  // CTO-298 follow-up: the route probes for first-event traffic, because the page's empty state is
  // a claim about this workspace and nothing here had ever measured it. Default to a workspace
  // that has sent something, so the cases below are about the replay corpus, not about traffic.
  queryFirstEventSeen: vi.fn().mockResolvedValue("connected"),
}));

import { GET as EstimateGET, POST as EstimatePOST } from "./route";
import * as ch from "@/lib/clickhouse";
import { projection } from "@/lib/estimate";

const queryReplayEstimate = ch.queryReplayEstimate as unknown as ReturnType<typeof vi.fn>;
const queryReplayCandidates = ch.queryReplayCandidates as unknown as ReturnType<typeof vi.fn>;
const queryFirstEventSeen = ch.queryFirstEventSeen as unknown as ReturnType<typeof vi.fn>;

const originalDemo = process.env.NEXT_PUBLIC_DEMO_MODE;
const originalDev = process.env.TALLY_DEV_TENANT;

/** A demo build with no organization resolved: the only configuration the fixture is allowed in. */
function demoBuild() {
  process.env.NEXT_PUBLIC_DEMO_MODE = "1";
  process.env.TALLY_DEV_TENANT = "00000000-0000-0000-0000-000000000000";
}

/** A real signed-in tenant: a Clerk organization resolved, so no dev-tenant escape hatch. */
function realTenant() {
  delete process.env.NEXT_PUBLIC_DEMO_MODE;
  delete process.env.TALLY_DEV_TENANT;
}

/** The fixture's tells. None of these may appear in a real tenant's payload. */
function expectNoFixtureFigures(body: unknown) {
  const wire = JSON.stringify(body);
  expect(wire).not.toContain("19100000000"); // the $19,100/mo baseline
  expect(wire).not.toContain("1284"); // the invented pull request
  expect(wire).not.toContain("0.42"); // the blow-up risk
  expect(wire).not.toContain("research_agent");
}

function postReq(body: unknown) {
  return new Request("http://test/api/estimate", {
    method: "POST",
    body: JSON.stringify(body),
  }) as never;
}

const getReq = (qs = "") =>
  new Request(`http://test/api/estimate${qs}`) as never;

/** One well-grounded candidate row, 60 replayed samples: comfortably over the 50-sample floor. */
const GROUNDED = {
  samples_available: 120,
  per_candidate: [
    {
      provider: "anthropic",
      model: "claude-haiku-4-5",
      projected_monthly_cost_micro_usd: 5_000_000,
      p50_latency_ms: 800,
      p95_latency_ms: 1500,
      error_rate: 0.01,
      samples_replayed: 60,
      excluded_budget_count: 0,
    },
  ],
  diagnostics: {
    context_fidelity: "resolved-context replay (no live retrieval)",
    replay_cost_micro_usd: 1000,
  },
};

beforeEach(realTenant);

afterEach(() => {
  vi.clearAllMocks();
  if (originalDemo === undefined) delete process.env.NEXT_PUBLIC_DEMO_MODE;
  else process.env.NEXT_PUBLIC_DEMO_MODE = originalDemo;
  if (originalDev === undefined) delete process.env.TALLY_DEV_TENANT;
  else process.env.TALLY_DEV_TENANT = originalDev;
});

// CTO-298. The gate itself is pinned alongside every other fixture route in
// api/three-states.test.ts; these cases pin what each side of it RETURNS.
describe("GET /api/estimate: the sample gate", () => {
  it("serves nothing from the fixture to a real tenant with no replay corpus", async () => {
    const res = await EstimateGET(getReq());
    const body = await res.json();

    expect(body.synthetic).toBe(false);
    expect(body.replay_source).toBe("none");
    expect(body.workload).toBeNull();
    expect(body.pr).toBeNull();
    expect(body.blowUpRisk).toBeNull();
    expect(body.drivers).toEqual([]);
    // Null, never 0: a baseline nobody measured is unknown, and "$0.00/mo" is a claim.
    expect(body.current.monthlyCostMicroUsd).toBeNull();
    expect(body.current.p99CostMicroUsd).toBeNull();
    expect(body.current.meanLatencyMs).toBeNull();
    expectNoFixtureFigures(body);
  });

  it("still serves the fixture, labelled synthetic, for a demo build with no organization", async () => {
    demoBuild();
    const res = await EstimateGET(getReq());
    const body = await res.json();

    expect(body.synthetic).toBe(true);
    expect(body.replay_source).toBe("mock");
    expect(body.current.monthlyCostMicroUsd).toBe(projection.current.monthlyCostMicroUsd);
    expect(body.pr.number).toBe(1284);
    expect(body.blowUpRisk).toBe(projection.blowUpRisk);
    expect(body.drivers.length).toBe(projection.drivers.length);
  });

  it("refuses the fixture when a candidate is named but no replay exists", async () => {
    queryReplayCandidates.mockResolvedValueOnce(null);
    const res = await EstimateGET(getReq("?candidate_model=claude-haiku-4-5"));
    const body = await res.json();
    expect(body.replay_source).toBe("none");
    expectNoFixtureFigures(body);
  });
});

describe("GET /api/estimate: a real replay", () => {
  it("carries the replayed figures and none of the fixture's", async () => {
    queryReplayCandidates.mockResolvedValueOnce(GROUNDED);
    const res = await EstimateGET(getReq("?candidate_model=claude-haiku-4-5"));
    const body = await res.json();

    expect(body.replay_source).toBe("replay");
    expect(body.proposed.monthlyCostMicroUsd).toBe(5_000_000);
    expect(body.sample.used).toBe(60);
    // The regression this ticket exists for: the branch used to spread the fixture, so a genuine
    // replay result still shipped the invented PR, risk, drivers and baseline around it.
    expect(body.synthetic).toBe(false);
    expect(body.pr).toBeNull();
    expect(body.blowUpRisk).toBeNull();
    expect(body.drivers).toEqual([]);
    expectNoFixtureFigures(body);
  });

  // CTO-298: two numbers were manufactured from inputs that do not contain them.
  it("reports no p99 cost and no mean latency rather than deriving them", async () => {
    queryReplayCandidates.mockResolvedValueOnce(GROUNDED);
    const res = await EstimateGET(getReq("?candidate_model=claude-haiku-4-5"));
    const body = await res.json();

    // Was Math.round(monthly * 1.4): a fixture multiplier wearing a percentile's name.
    expect(body.proposed.p99CostMicroUsd).toBeNull();
    expect(body.proposed.p99CostMicroUsd).not.toBe(Math.round(5_000_000 * 1.4));
    // Was the replay row's p50 (800ms), presented to the reader as a mean.
    expect(body.proposed.meanLatencyMs).toBeNull();
    expect(body.proposed.meanLatencyMs).not.toBe(800);
  });
});

// CTO-298 follow-up. The page's empty state used to be derived from the null baseline this route
// returns to every real tenant, so a pilot with a live corpus was told nothing had ever arrived.
// The route now reports what the first-event probe measured, and the page keys on that instead.
describe("GET /api/estimate: the workspace-traffic probe", () => {
  it("reports traffic the probe found, so the page renders the what-if rather than an empty state", async () => {
    queryFirstEventSeen.mockResolvedValueOnce("connected");
    const res = await EstimateGET(getReq());
    const body = await res.json();

    expect(body.workspaceTraffic).toBe("connected");
    // Still no baseline: an estimate needs a replayed corpus, and that is a separate fact from
    // whether the workspace has traffic. The two were conflated, which is the bug.
    expect(body.current.monthlyCostMicroUsd).toBeNull();
  });

  it("reports an empty workspace only when the probe actually found none", async () => {
    queryFirstEventSeen.mockResolvedValueOnce("waiting");
    const res = await EstimateGET(getReq());
    expect((await res.json()).workspaceTraffic).toBe("waiting");
  });

  it("passes the probe's unknown through rather than collapsing it onto 'nothing here'", async () => {
    // A probe that could not run is the honest-unknown case. Folding it onto "waiting" would turn
    // an absence of knowledge into a definite negative, which is the invariant this route breaks.
    queryFirstEventSeen.mockResolvedValueOnce("unknown");
    const res = await EstimateGET(getReq());
    expect((await res.json()).workspaceTraffic).toBe("unknown");
  });

  it("carries the probe's answer onto a real replay result too", async () => {
    queryFirstEventSeen.mockResolvedValueOnce("connected");
    queryReplayCandidates.mockResolvedValueOnce(GROUNDED);
    const res = await EstimateGET(getReq("?candidate_model=claude-haiku-4-5"));
    const body = await res.json();
    expect(body.workspaceTraffic).toBe("connected");
    expect(body.sample.used).toBe(60);
  });

  // CTO-298 follow-up: the "samples used" diagnostic rendered this 0 as though it were a count.
  it("reports no sample count at all when no replay was attempted", async () => {
    const res = await EstimateGET(getReq());
    const body = await res.json();
    // Was 0 from EMPTY_PROJECTION, which reads as "we replayed and used none of it".
    expect(body.sample.used).toBeNull();
    expect(body.sample.used).not.toBe(0);
  });
});

describe("POST /api/estimate", () => {
  it("400s when candidateModel is missing", async () => {
    const res = await EstimatePOST(postReq({ systemPromptOverride: "x" }));
    expect(res.status).toBe(400);
  });

  it("maps a well-grounded projection into the proposed shape", async () => {
    queryReplayEstimate.mockResolvedValueOnce(GROUNDED);

    const res = await EstimatePOST(
      postReq({ candidateModel: "claude-haiku-4-5", systemPromptOverride: "tighter prompt" }),
    );
    const body = await res.json();
    expect(body.replay_source).toBe("replay");
    expect(body.proposed.monthlyCostMicroUsd).toBe(5_000_000);
    // CTO-298: neither is derivable from a per-call replay, so neither is claimed.
    expect(body.proposed.p99CostMicroUsd).toBeNull();
    expect(body.proposed.meanLatencyMs).toBeNull();
    expect(body.groundedSamples).toBe(60);
    expect(body.candidate).toEqual({ provider: "anthropic", model: "claude-haiku-4-5" });
    // A real tenant's what-if carries the replayed figures and nothing borrowed.
    expectNoFixtureFigures(body);

    // The override + candidate were forwarded to the gateway helper.
    expect(queryReplayEstimate).toHaveBeenCalledWith(
      expect.objectContaining({
        candidateModel: { provider: "anthropic", model: "claude-haiku-4-5" },
        systemPromptOverride: "tighter prompt",
      }),
    );
  });

  it("applies the honest-null floor when fewer than 50 samples ground the estimate", async () => {
    queryReplayEstimate.mockResolvedValueOnce({
      ...GROUNDED,
      samples_available: 40,
      per_candidate: [{ ...GROUNDED.per_candidate[0], samples_replayed: 40 }],
    });

    const res = await EstimatePOST(postReq({ candidateModel: "claude-haiku-4-5" }));
    const body = await res.json();
    // "none", not "mock": for a real tenant there is no fixture behind the blanks to describe.
    expect(body.replay_source).toBe("none");
    expect(body.proposed.monthlyCostMicroUsd).toBeNull();
    expect(body.proposed.p99CostMicroUsd).toBeNull();
    expect(body.proposed.meanLatencyMs).toBeNull();
    expect(body.groundedSamples).toBe(40);
    expectNoFixtureFigures(body);
  });

  it("applies the honest-null floor when the gateway returns null (no corpus / unreachable)", async () => {
    queryReplayEstimate.mockResolvedValueOnce(null);
    const res = await EstimatePOST(postReq({ candidateModel: "gpt-5-mini", providerOverride: "openai" }));
    const body = await res.json();
    expect(body.proposed.monthlyCostMicroUsd).toBeNull();
    // CTO-298 follow-up: null, not 0. No replay row came back, so no sample count was taken, and a
    // 0 here would claim a replay ran and matched nothing.
    expect(body.groundedSamples).toBeNull();
    expect(body.sample.used).toBeNull();
    expect(body.replay_source).toBe("none");
    expectNoFixtureFigures(body);
  });

  it("keeps the fixture context on the demo path, where it is labelled", async () => {
    demoBuild();
    queryReplayEstimate.mockResolvedValueOnce(null);
    const res = await EstimatePOST(postReq({ candidateModel: "claude-haiku-4-5" }));
    const body = await res.json();
    expect(body.synthetic).toBe(true);
    expect(body.replay_source).toBe("mock");
    expect(body.current.monthlyCostMicroUsd).toBe(projection.current.monthlyCostMicroUsd);
  });
});
