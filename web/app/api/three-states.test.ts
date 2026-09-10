// SPDX-License-Identifier: Apache-2.0
// #364. The six dashboard routes must tell three facts apart, and answer each of them differently:
//
//   unavailable - the read threw or timed out. We do not know. Say so; claim no figure.
//   empty       - the read succeeded over no rows. We DO know: nothing has arrived. This is the
//                 normal state of every new customer, and it is a real answer, not an unknown one.
//   live        - real rows, passed through untouched.
//
// They were one branch before this ticket (`live ?? mock`, or `live.length > 0 ? live : mock`), so a
// brand new tenant with zero spans was served `lib/mock.ts` as its own numbers: a research_agent it
// had never run, paying back in 7 days. Every case below pins one of the three, deterministically,
// over a mocked ClickHouse: `api.test.ts` cannot, because whether a developer has `make up` running
// decides which of empty/unavailable a real read produces there.
//
// The fourth state, `sample`, is a deployment choice rather than a read result, and its gate is
// pinned at the bottom: it takes an explicit demo build AND no resolved Clerk organization, which
// makes "a real tenant is signed in" and "fixtures render" mutually exclusive by construction.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted above the route imports below, so each route handler picks up these stubs.
vi.mock("@/lib/clickhouse", () => ({
  querySpendSummary: vi.fn(),
  queryRoi: vi.fn(),
  queryDataQuality: vi.fn(),
  queryAttribution: vi.fn(),
  queryAgents: vi.fn(),
  queryReconcilerLastRun: vi.fn().mockResolvedValue(null),
  queryCostSeries: vi.fn(),
  queryFeatureCostRows: vi.fn(),
  queryHiddenCostAlerts: vi.fn(),
  queryFeatureEconomics: vi.fn(),
  queryAttributionDiagnostics: vi.fn(),
  queryFeatureValueEvents: vi.fn().mockResolvedValue([]),
  queryConnectorActivity: vi.fn(),
  queryIntegrationStatus: vi.fn().mockResolvedValue([]),
}));

import * as ch from "@/lib/clickhouse";
import { agents as fixtureAgents, runs as fixtureRuns } from "@/lib/agents";
import {
  LAYERS,
  costSeries as fixtureSeries,
  featureRows as fixtureFeatureRows,
  hiddenCostAlerts as fixtureAlerts,
} from "@/lib/cost";
import { features as fixtureFeatures } from "@/lib/features";
import { mockActivity } from "@/lib/connectors";
import { mockRoi, mockSpend } from "@/lib/mock";
import type { SpendByLayer, SpendSummary } from "@/lib/types";

import { GET as HomeGET } from "./home/route";
import { GET as AgentsGET } from "./agents/route";
import { GET as CostGET } from "./cost/route";
import { GET as FeaturesGET } from "./features/route";
import { GET as AttributionGET } from "./attribution/route";
import { GET as ConnectorsGET } from "./connectors/route";

const mocked = ch as unknown as Record<string, ReturnType<typeof vi.fn>>;

/** Every layer at zero: what an aggregate over no spans genuinely produces. */
function zeroLayers(): SpendByLayer {
  const out = {} as SpendByLayer;
  for (const l of LAYERS) out[l] = 0;
  return out;
}


/** Every telemetry read fails. */
function allUnavailable() {
  for (const fn of Object.values(mocked)) fn.mockResolvedValue(null);
  mocked.queryFeatureValueEvents.mockResolvedValue([]);
  mocked.queryAttributionDiagnostics.mockResolvedValue({ state: "unavailable" });
}

/**
 * Every read succeeds and finds nothing. The important shapes here are the ones that are NOT an
 * empty array: a spend summary is always an object, and a cost series is always one point per day
 * in the window, so both come back as a wall of confident zeros that only the counts disprove.
 */
const EMPTY_SPEND: SpendSummary = {
  totalMicroUsd: 0,
  estimatedMicroUsd: 0,
  reconciledMicroUsd: 0,
  reconciledThrough: "1970-01-01",
  byLayer: zeroLayers(),
  unpricedSpanCount: 0,
  spanCount: 0,
};

function allEmpty() {
  mocked.querySpendSummary.mockResolvedValue(EMPTY_SPEND);
  mocked.queryRoi.mockResolvedValue([]);
  mocked.queryDataQuality.mockResolvedValue({
    attributionRate: null,
    contextDropCount: null,
    estimateCalibration: null,
  });
  mocked.queryAttribution.mockResolvedValue({
    filters: { tag: null, provider: null, outcome: null },
    perProvider: [],
    totals: { sessions: 0, conversions: 0, costMicroUsd: 0, costPerConversionMicroUsd: null },
    dailyByProvider: [],
    isMock: false,
  });
  mocked.queryAgents.mockResolvedValue({ agents: [], runs: [] });
  mocked.queryCostSeries.mockResolvedValue({
    reconciledThrough: "1970-01-01",
    days: Array.from({ length: 30 }, (_, i) => ({
      date: `2026-06-${String(i + 1).padStart(2, "0")}`,
      byLayer: zeroLayers(),
    })),
  });
  mocked.queryFeatureCostRows.mockResolvedValue([]);
  mocked.queryHiddenCostAlerts.mockResolvedValue([]);
  mocked.queryFeatureEconomics.mockResolvedValue([]);
  mocked.queryFeatureValueEvents.mockResolvedValue([]);
  mocked.queryAttributionDiagnostics.mockResolvedValue({ state: "empty" });
  mocked.queryConnectorActivity.mockResolvedValue({ records: {}, lastAt: {} });
  mocked.queryIntegrationStatus.mockResolvedValue([]);
}

/** Real rows. The library fixtures stand in for them: what is asserted is pass-through, not content. */
function allLive() {
  allEmpty();
  mocked.querySpendSummary.mockResolvedValue({ ...EMPTY_SPEND, totalMicroUsd: 12_000_000, spanCount: 4_211, reconciledThrough: "2026-06-12" });
  mocked.queryRoi.mockResolvedValue([
    { feature: "checkout_helper", costPerUserMicroUsd: 1_000, valuePerUserMicroUsd: 9_000, paybackDays: 3, attributionRate: 0.5 },
  ]);
  mocked.queryDataQuality.mockResolvedValue({
    attributionRate: 0.5,
    contextDropCount: null,
    estimateCalibration: null,
  });
  mocked.queryAgents.mockResolvedValue({ agents: fixtureAgents, runs: fixtureRuns });
  mocked.queryCostSeries.mockResolvedValue(fixtureSeries);
  mocked.queryFeatureCostRows.mockResolvedValue(fixtureFeatureRows);
  mocked.queryHiddenCostAlerts.mockResolvedValue(fixtureAlerts);
  mocked.queryFeatureEconomics.mockResolvedValue(fixtureFeatures);
  mocked.queryAttributionDiagnostics.mockResolvedValue({
    state: "live",
    diagnostics: { lateArrivalEvents7d: 4, lateArrivalMedianHours: 1.1, reconcilerLastRunMinutesAgo: 9 },
  });
  mocked.queryConnectorActivity.mockResolvedValue({
    records: { llm_proxy: 7 },
    lastAt: { llm_proxy: "2026-06-12T00:00:00Z" },
  });
  mocked.queryAttribution.mockResolvedValue({
    filters: { tag: null, provider: null, outcome: null },
    perProvider: [
      { provider: "openai", sessions: 10, conversions: 2, conversionRate: 0.2, costMicroUsd: 500, costPerConversionMicroUsd: 250 },
    ],
    totals: { sessions: 10, conversions: 2, costMicroUsd: 500, costPerConversionMicroUsd: 250 },
    dailyByProvider: [],
    isMock: false,
  });
}

async function body<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

const home = () => HomeGET(new Request("http://test/api/home"));
const agents = () => AgentsGET(new Request("http://test/api/agents") as never);
const cost = () => CostGET(new Request("http://test/api/cost") as never);
const attribution = () => AttributionGET(new Request("http://test/api/attribution"));

afterEach(() => {
  vi.clearAllMocks();
});

describe("the read failed: the routes say so and claim nothing", () => {
  beforeEach(allUnavailable);

  it("/api/home reports unavailable and returns no spend, no ROI, no data quality", async () => {
    const b = await body<{ spend: unknown; roi: unknown[]; dq: unknown; sources: Record<string, string> }>(await home());
    expect(b.sources).toEqual({ spend: "unavailable", roi: "unavailable", dq: "unavailable", attribution: "unavailable" });
    // Null, not a zero-filled summary: a $0.00 headline is a measurement, and none was taken.
    expect(b.spend).toBeNull();
    expect(b.dq).toBeNull();
    expect(b.roi).toEqual([]);
    // The regression this ticket exists for.
    expect(b.spend).not.toEqual(mockSpend);
    expect(b.roi).not.toEqual(mockRoi);
  });

  it("/api/agents reports unavailable and lists no agents", async () => {
    const b = await body<{ agents: unknown[]; runs: unknown[]; sources: Record<string, string> }>(await agents());
    expect(b.sources).toEqual({ agents: "unavailable", runs: "unavailable" });
    expect(b.agents).toEqual([]);
    expect(b.runs).toEqual([]);
  });

  it("/api/cost reports unavailable and returns a null series rather than 30 zero days", async () => {
    const b = await body<{ series: unknown; featureRows: unknown[]; alerts: unknown[]; sources: Record<string, string> }>(await cost());
    expect(b.sources).toEqual({ series: "unavailable", featureRows: "unavailable", alerts: "unavailable" });
    expect(b.series).toBeNull();
    expect(b.featureRows).toEqual([]);
    expect(b.alerts).toEqual([]);
  });

  it("/api/features reports unavailable and returns no economics and no diagnostics", async () => {
    const b = await body<{ features: unknown[]; diagnostics: unknown; sources: Record<string, string> }>(await FeaturesGET());
    expect(b.sources).toEqual({ features: "unavailable", diagnostics: "unavailable" });
    expect(b.features).toEqual([]);
    expect(b.diagnostics).toBeNull();
  });

  it("/api/attribution reports unavailable and invents no provider rows", async () => {
    const b = await body<{ state: string; isMock: boolean; perProvider: unknown[]; totals: { sessions: number } }>(await attribution());
    expect(b.state).toBe("unavailable");
    expect(b.isMock).toBe(false);
    expect(b.perProvider).toEqual([]);
    expect(b.totals.sessions).toBe(0);
  });

  it("/api/connectors reports unavailable activity and credits no records to any connector", async () => {
    const b = await body<{ activity: string; connectors: { records: number }[] }>(await ConnectorsGET());
    expect(b.activity).toBe("unavailable");
    // Every catalog row still renders, with no fabricated record count behind it.
    expect(b.connectors.length).toBeGreaterThan(0);
    expect(b.connectors.every((c) => c.records === 0)).toBe(true);
  });
});

describe("the read succeeded over nothing: a real empty answer, distinct from unknown", () => {
  beforeEach(allEmpty);

  it("/api/home reports empty, not unavailable, and still returns no figures", async () => {
    const b = await body<{ spend: SpendSummary | null; roi: unknown[]; sources: Record<string, string> }>(await home());
    expect(b.sources.spend).toBe("empty");
    expect(b.sources.roi).toBe("empty");
    expect(b.sources.dq).toBe("empty");
    expect(b.sources.attribution).toBe("empty");
    // The zero-filled summary is carried, because it is what the read genuinely found. The page is
    // told to render an empty state off `sources`, not to print these zeros as a measurement.
    expect(b.spend?.spanCount).toBe(0);
    expect(b.roi).toEqual([]);
  });

  it("/api/agents reports empty", async () => {
    const b = await body<{ sources: Record<string, string> }>(await agents());
    expect(b.sources).toEqual({ agents: "empty", runs: "empty" });
  });

  it("/api/cost reports empty for a full window of zero days", async () => {
    const b = await body<{ series: { days: unknown[] } | null; sources: Record<string, string> }>(await cost());
    expect(b.sources.series).toBe("empty");
    expect(b.sources.featureRows).toBe("empty");
    expect(b.sources.alerts).toBe("empty");
    // The shape said nothing: 30 points came back. Only the figures in them make it empty.
    expect(b.series?.days.length).toBe(30);
  });

  it("/api/features reports empty features and an empty (never-run) reconciler", async () => {
    const b = await body<{ diagnostics: unknown; sources: Record<string, string> }>(await FeaturesGET());
    expect(b.sources).toEqual({ features: "empty", diagnostics: "empty" });
    // "The reconciler has never run" is not "the reconciler reported 180 late events".
    expect(b.diagnostics).toBeNull();
  });

  it("/api/attribution reports empty", async () => {
    const b = await body<{ state: string; perProvider: unknown[] }>(await attribution());
    expect(b.state).toBe("empty");
    expect(b.perProvider).toEqual([]);
  });

  it("/api/connectors reports empty activity: every row is honestly Not connected", async () => {
    const b = await body<{ activity: string; connectors: { records: number }[] }>(await ConnectorsGET());
    expect(b.activity).toBe("empty");
    expect(b.connectors.every((c) => c.records === 0)).toBe(true);
  });
});

describe("real rows: passed through untouched", () => {
  beforeEach(allLive);

  it("/api/home reports live and serves the tenant's own spend and ROI", async () => {
    const b = await body<{ spend: SpendSummary; roi: { feature: string }[]; sources: Record<string, string> }>(await home());
    expect(b.sources.spend).toBe("live");
    expect(b.sources.roi).toBe("live");
    expect(b.spend.totalMicroUsd).toBe(12_000_000);
    expect(b.roi.map((r) => r.feature)).toEqual(["checkout_helper"]);
  });

  it("/api/agents reports live and serves the rows the query returned", async () => {
    const b = await body<{ agents: unknown[]; runs: unknown[]; sources: Record<string, string> }>(await agents());
    expect(b.sources).toEqual({ agents: "live", runs: "live" });
    expect(b.agents).toEqual(JSON.parse(JSON.stringify(fixtureAgents)));
    expect(b.runs).toEqual(JSON.parse(JSON.stringify(fixtureRuns)));
  });

  it("/api/cost reports live and serves the series, rows and alerts it read", async () => {
    const b = await body<{ series: { days: unknown[] }; featureRows: unknown[]; alerts: unknown[]; sources: Record<string, string> }>(await cost());
    expect(b.sources).toEqual({ series: "live", featureRows: "live", alerts: "live" });
    expect(b.series.days.length).toBe(fixtureSeries.days.length);
    expect(b.featureRows.length).toBe(fixtureFeatureRows.length);
    expect(b.alerts.length).toBe(fixtureAlerts.length);
  });

  it("/api/features reports live and serves real diagnostics", async () => {
    const b = await body<{ features: unknown[]; diagnostics: { reconcilerLastRunMinutesAgo: number }; sources: Record<string, string> }>(await FeaturesGET());
    expect(b.sources).toEqual({ features: "live", diagnostics: "live" });
    expect(b.features.length).toBe(fixtureFeatures.length);
    expect(b.diagnostics.reconcilerLastRunMinutesAgo).toBe(9);
  });

  it("/api/attribution reports live", async () => {
    const b = await body<{ state: string; perProvider: { provider: string }[] }>(await attribution());
    expect(b.state).toBe("live");
    expect(b.perProvider.map((p) => p.provider)).toEqual(["openai"]);
  });

  it("/api/connectors reports live activity", async () => {
    const b = await body<{ activity: string; connectors: { id: string; records: number }[] }>(await ConnectorsGET());
    expect(b.activity).toBe("live");
    expect(b.connectors.find((c) => c.id === "llm_proxy")?.records).toBe(7);
  });
});

/**
 * The gate that makes the fixtures unreachable on the product path.
 *
 * `sampleDataAllowed()` needs BOTH an explicit demo build and the dev-tenant escape hatch, and the
 * escape hatch is the ONLY way the dashboard serves a tenant with no Clerk organization resolved
 * (lib/getTenant.ts), so a signed-in customer can never reach these branches. A production build
 * refuses to boot on the escape hatch alone (instrumentation.ts, CTO-268), which closes it twice.
 */
describe("the sample gate", () => {
  const originalDemo = process.env.NEXT_PUBLIC_DEMO_MODE;
  const originalDev = process.env.TALLY_DEV_TENANT;

  beforeEach(() => {
    // Empty reads throughout: without the gate every route below would answer honestly.
    allEmpty();
  });

  afterEach(() => {
    if (originalDemo === undefined) delete process.env.NEXT_PUBLIC_DEMO_MODE;
    else process.env.NEXT_PUBLIC_DEMO_MODE = originalDemo;
    if (originalDev === undefined) delete process.env.TALLY_DEV_TENANT;
    else process.env.TALLY_DEV_TENANT = originalDev;
  });

  it("serves fixtures, labelled, for a demo build with no organization resolved", async () => {
    process.env.NEXT_PUBLIC_DEMO_MODE = "1";
    process.env.TALLY_DEV_TENANT = "00000000-0000-0000-0000-000000000000";

    const h = await body<{ spend: unknown; roi: unknown; sources: Record<string, string> }>(await home());
    expect(h.sources.spend).toBe("sample");
    expect(h.spend).toEqual(mockSpend);
    expect(h.roi).toEqual(mockRoi);

    const c = await body<{ activity: string; connectors: { id: string; records: number }[] }>(await ConnectorsGET());
    expect(c.activity).toBe("sample");
    expect(c.connectors.find((x) => x.id === "llm_proxy")?.records).toBe(mockActivity.records.llm_proxy);

    const a = await body<{ state: string; isMock: boolean }>(await attribution());
    expect(a.state).toBe("sample");
    expect(a.isMock).toBe(true);

    const co = await body<{ sources: Record<string, string> }>(await cost());
    expect(co.sources.series).toBe("sample");
    const ag = await body<{ sources: Record<string, string> }>(await agents());
    expect(ag.sources.agents).toBe("sample");
    const f = await body<{ sources: Record<string, string> }>(await FeaturesGET());
    expect(f.sources.features).toBe("sample");
  });

  it("refuses fixtures for a demo build once a real organization resolves the tenant", async () => {
    process.env.NEXT_PUBLIC_DEMO_MODE = "1";
    delete process.env.TALLY_DEV_TENANT;

    // This is the case the ticket is about: an authenticated customer with no spans. Demo mode is
    // still on, and it buys them nothing, because there is a real tenant behind the request.
    const h = await body<{ spend: SpendSummary | null; roi: unknown[]; sources: Record<string, string> }>(await home());
    expect(h.sources.spend).toBe("empty");
    expect(h.spend).not.toEqual(mockSpend);
    expect(h.roi).toEqual([]);

    const a = await body<{ state: string; isMock: boolean; perProvider: unknown[] }>(await attribution());
    expect(a.state).toBe("empty");
    expect(a.isMock).toBe(false);
    expect(a.perProvider).toEqual([]);

    const c = await body<{ activity: string; connectors: { records: number }[] }>(await ConnectorsGET());
    expect(c.activity).toBe("empty");
    expect(c.connectors.every((x) => x.records === 0)).toBe(true);
  });

  it("refuses fixtures for the dev escape hatch when demo mode is not switched on", async () => {
    delete process.env.NEXT_PUBLIC_DEMO_MODE;
    process.env.TALLY_DEV_TENANT = "00000000-0000-0000-0000-000000000000";

    const h = await body<{ sources: Record<string, string> }>(await home());
    expect(h.sources.spend).toBe("empty");
  });

  it("keeps fixtures out of a filtered view even in a demo build", async () => {
    process.env.NEXT_PUBLIC_DEMO_MODE = "1";
    process.env.TALLY_DEV_TENANT = "00000000-0000-0000-0000-000000000000";

    // The fixture roster is not tag-scoped, so serving it under ?tag= would present unfiltered
    // fixtures as the answer to a filtered question. This guard predates the ticket and survives it.
    const b = await body<{ agents: unknown[]; sources: Record<string, string> }>(
      await AgentsGET(new Request("http://test/api/agents?tag=nothing-matches") as never),
    );
    expect(b.sources.agents).toBe("empty");
    expect(b.agents).toEqual([]);
  });
});
