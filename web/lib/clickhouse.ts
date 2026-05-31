// Live ClickHouse reads for the dashboard (server-only).
//
// The Route Handlers call these and fall back to mock data when ClickHouse is unreachable (no
// stack running, CI, fresh clone) — so `npm run dev/build/test` never depend on infra. Money comes
// back from ClickHouse as Decimal strings; we convert to integer micro-USD at the boundary to match
// the wire/UI contract.
//
// Today only LLM spans exist in `otel_spans`, so cost lands in the `llm`/`tools`/`embeddings`
// layers; vector/compute/egress are zero until those sources are instrumented. That's honest: the
// dashboard shows exactly the telemetry that exists.
//
// Server-only: imported solely by Route Handlers pinned to the nodejs runtime (never a client
// component), so it never reaches the browser bundle.

import { createClient, type ClickHouseClient } from "@clickhouse/client";

import type {
  CostOutlier,
  DataQuality,
  FeatureRoi,
  SpendByLayer,
  SpendSummary,
} from "./types";
import type { CostDayPoint, CostSeries, FeatureCostRow } from "./cost";
import { LAYERS, type Layer } from "./cost";
import type { AttributionDiagnostics, FeatureEconomics } from "./features";
import type {
  AttributionByFeature,
  CalibrationDay,
  ContextDropsByService,
  DataQualityReport,
  SampleByStratum,
} from "./dq";
import type { AgentRun, AgentSummary, RunSpan } from "./agents";
import { CONNECTORS, type ConnectorActivity } from "./connectors";

const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";

let _client: ClickHouseClient | null = null;

function client(): ClickHouseClient {
  if (_client === null) {
    _client = createClient({
      url: process.env.TALLY_CLICKHOUSE_URL ?? "http://localhost:8123",
      username: process.env.TALLY_CLICKHOUSE_USER ?? "tally",
      password: process.env.TALLY_CLICKHOUSE_PASSWORD ?? "tally",
      database: process.env.TALLY_CLICKHOUSE_DB ?? "default",
      request_timeout: 4000,
    });
  }
  return _client;
}

/** Run `fn` against ClickHouse; return null on any failure so callers can fall back to mock. */
export async function tryLive<T>(fn: (db: ClickHouseClient, tenant: string) => Promise<T>): Promise<T | null> {
  try {
    return await fn(client(), TENANT);
  } catch (err) {
    console.warn("[clickhouse] live query failed, falling back to mock:", (err as Error).message);
    return null;
  }
}

// Decimal string (USD) -> integer micro-USD.
function micro(decimalUsd: string | number | null | undefined): number {
  const n = typeof decimalUsd === "number" ? decimalUsd : parseFloat(decimalUsd ?? "0");
  return Math.round((Number.isFinite(n) ? n : 0) * 1_000_000);
}

function zeroLayers(): SpendByLayer {
  return { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 };
}

// Map a gen_ai operation to a cost layer. Only LLM-family spans exist today.
const LAYER_CASE =
  "multiIf(GenAiOperation = 'tool', 'tools', GenAiOperation = 'embeddings', 'embeddings', 'llm')";

async function rows<T>(db: ClickHouseClient, query: string, tenant: string): Promise<T[]> {
  const rs = await db.query({
    query,
    query_params: { tenant },
    format: "JSONEachRow",
  });
  return rs.json<T>();
}

// Like `rows` but allows extra named query params (e.g. an Array(String) of trace ids).
async function rowsP<T>(
  db: ClickHouseClient,
  query: string,
  params: Record<string, unknown>,
): Promise<T[]> {
  const rs = await db.query({ query, query_params: params, format: "JSONEachRow" });
  return rs.json<T>();
}

function median(xs: number[]): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function quantile(xs: number[], q: number): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const idx = Math.min(s.length - 1, Math.max(0, Math.round(q * (s.length - 1))));
  return s[idx];
}

// --- Home / spend -------------------------------------------------------------------------------

export async function querySpendSummary(): Promise<SpendSummary | null> {
  return tryLive(async (db, tenant) => {
    const totals = await rows<{ total: string; estimated: string; reconciled: string; recThrough: string | null }>(
      db,
      `SELECT
         sum(EstimatedCost) AS total,
         sumIf(EstimatedCost, CostSource = 'estimated') AS estimated,
         sumIf(EstimatedCost, CostSource = 'reconciled') AS reconciled,
         toString(maxOrNull(if(CostSource = 'reconciled', toDate(Timestamp), NULL))) AS recThrough
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY`,
      tenant,
    );
    const byLayerRows = await rows<{ layer: Layer; cost: string }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY
       GROUP BY layer`,
      tenant,
    );
    const byLayer = zeroLayers();
    for (const r of byLayerRows) {
      if ((LAYERS as readonly string[]).includes(r.layer)) byLayer[r.layer] = micro(r.cost);
    }
    const t = totals[0] ?? { total: "0", estimated: "0", reconciled: "0", recThrough: null };
    return {
      totalMicroUsd: micro(t.total),
      estimatedMicroUsd: micro(t.estimated),
      reconciledMicroUsd: micro(t.reconciled),
      // No reconciled data yet → boundary in the far past so everything reads as estimated.
      reconciledThrough: t.recThrough && t.recThrough !== "\\N" ? t.recThrough : "1970-01-01",
      byLayer,
    };
  });
}

export async function queryOutliers(): Promise<CostOutlier[] | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ runId: string; agent: string; cost: string; mult: string | null }>(
      db,
      `WITH runs AS (
         SELECT TraceId AS runId, any(FeatureTag) AS agent, sum(EstimatedCost) AS cost
         FROM otel_spans
         WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 24 HOUR
         GROUP BY TraceId
       )
       SELECT runId, agent, cost,
              cost / nullIf((SELECT quantileExact(0.5)(cost) FROM runs), 0) AS mult
       FROM runs
       ORDER BY cost DESC
       LIMIT 5`,
      tenant,
    );
    return out.map((r) => ({
      runId: r.runId,
      agent: r.agent || "untagged",
      costMicroUsd: micro(r.cost),
      multipleOfMedian: r.mult ? Math.round(parseFloat(r.mult) * 10) / 10 : 1,
    }));
  });
}

export async function queryRoi(): Promise<FeatureRoi[] | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ feature: string; cost: string; users: string }>(
      db,
      `SELECT FeatureTag AS feature, sum(EstimatedCost) AS cost, uniqExact(UserIdHash) AS users
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != ''
       GROUP BY FeatureTag
       ORDER BY cost DESC`,
      tenant,
    );
    return out.map((r) => {
      const users = Math.max(1, parseInt(r.users, 10) || 1);
      return {
        feature: r.feature,
        costPerUserMicroUsd: Math.round(micro(r.cost) / users),
        // value/payback/attribution require business-event attribution (not wired yet) → null.
        valuePerUserMicroUsd: null,
        paybackDays: null,
        attributionRate: null,
      };
    });
  });
}

export async function queryDataQuality(): Promise<DataQuality | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ attributed: string; total: string }>(
      db,
      `SELECT
         (SELECT count() FROM attribution_records WHERE TenantId = {tenant:String}) AS attributed,
         (SELECT count() FROM business_events WHERE TenantId = {tenant:String}) AS total`,
      tenant,
    );
    const attributed = parseInt(out[0]?.attributed ?? "0", 10);
    const total = parseInt(out[0]?.total ?? "0", 10);
    return {
      // No value events yet → nothing to miss, rate is vacuously 1.0.
      attributionRate: total > 0 ? attributed / total : 1,
      contextDropCount: 0,
      estimateCalibration: 0,
    };
  });
}

// --- Cost workflow ------------------------------------------------------------------------------

export async function queryCostSeries(): Promise<CostSeries | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ day: string; layer: Layer; cost: string }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS day, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 14 DAY
       GROUP BY day, layer
       ORDER BY day`,
      tenant,
    );
    // Pivot into one CostDayPoint per calendar day (fill gaps with zero layers).
    const byDay = new Map<string, CostDayPoint>();
    for (let i = 13; i >= 0; i--) {
      const d = new Date();
      d.setUTCDate(d.getUTCDate() - i);
      const iso = d.toISOString().slice(0, 10);
      byDay.set(iso, { date: iso, byLayer: zeroLayers() });
    }
    for (const r of out) {
      const point = byDay.get(r.day);
      if (point && (LAYERS as readonly string[]).includes(r.layer)) {
        point.byLayer[r.layer] = micro(r.cost);
      }
    }
    return {
      reconciledThrough: "1970-01-01", // nothing reconciled yet
      days: [...byDay.values()],
    };
  });
}

export async function queryFeatureCostRows(): Promise<FeatureCostRow[] | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ feature: string; layer: Layer; cost: string }>(
      db,
      `SELECT FeatureTag AS feature, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != ''
       GROUP BY feature, layer`,
      tenant,
    );
    const byFeature = new Map<string, FeatureCostRow>();
    for (const r of out) {
      let row = byFeature.get(r.feature);
      if (!row) {
        row = { feature: r.feature, byLayer: zeroLayers() };
        byFeature.set(r.feature, row);
      }
      if ((LAYERS as readonly string[]).includes(r.layer)) row.byLayer[r.layer] = micro(r.cost);
    }
    return [...byFeature.values()].sort(
      (a, b) =>
        LAYERS.reduce((s, l) => s + b.byLayer[l], 0) - LAYERS.reduce((s, l) => s + a.byLayer[l], 0),
    );
  });
}

// --- Features (ROI + attribution diagnostics) ---------------------------------------------------

export async function queryFeatureEconomics(): Promise<FeatureEconomics[] | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ feature: string; cost: string; users: string }>(
      db,
      `SELECT FeatureTag AS feature, sum(EstimatedCost) AS cost, uniqExact(UserIdHash) AS users
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != ''
       GROUP BY FeatureTag
       ORDER BY cost DESC`,
      tenant,
    );
    return out.map((r) => {
      const users = Math.max(1, parseInt(r.users, 10) || 1);
      return {
        feature: r.feature,
        // Value events / payback / attribution all require business-event attribution, which is not
        // wired yet — surface honest nulls rather than fabricated ROI.
        valueEvent: null,
        costPerUserMicroUsd: Math.round(micro(r.cost) / users),
        valuePerUserMicroUsd: null,
        paybackDays: null,
        attributionRate: null,
        attributionBreakdown: { direct: 0, sessionStitched: 0, identityGraphStitched: 0, unmatched: 0 },
      };
    });
  });
}

export async function queryAttributionDiagnostics(): Promise<AttributionDiagnostics | null> {
  // No reconciler / late-arrival pipeline runs yet, so every counter is honestly zero. We still
  // gate on ClickHouse being reachable (tryLive) so a live deployment shows live zeros, not mock.
  return tryLive(async (db, tenant) => {
    await rows(db, `SELECT 1 FROM otel_spans WHERE TenantId = {tenant:String} LIMIT 1`, tenant);
    return { lateArrivalEvents7d: 0, lateArrivalMedianHours: 0, reconcilerLastRunMinutesAgo: 0 };
  });
}

// --- Data Quality (dedicated report) ------------------------------------------------------------

export async function queryDataQualityReport(): Promise<DataQualityReport | null> {
  return tryLive(async (db, tenant) => {
    const attr = await rows<{ attributed: string; total: string }>(
      db,
      `SELECT
         (SELECT count() FROM attribution_records WHERE TenantId = {tenant:String}) AS attributed,
         (SELECT count() FROM business_events WHERE TenantId = {tenant:String}) AS total`,
      tenant,
    );
    const attributed = parseInt(attr[0]?.attributed ?? "0", 10);
    const totalEvents = parseInt(attr[0]?.total ?? "0", 10);

    const sample = await rows<{ rate: string }>(
      db,
      `SELECT avg(SampleRate) AS rate FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY`,
      tenant,
    );
    const effectiveSampleRate = parseFloat(sample[0]?.rate ?? "1") || 1;

    const perFeature = await rows<{ feature: string; events: string }>(
      db,
      `SELECT FeatureTag AS feature, count() AS events FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 7 DAY AND FeatureTag != ''
       GROUP BY feature ORDER BY events DESC`,
      tenant,
    );
    // No value attribution yet → per-feature rate is 0 (nothing matched), but event counts are real.
    const attribution: AttributionByFeature[] = perFeature.map((r) => ({
      feature: r.feature,
      rate: 0,
      events7d: parseInt(r.events, 10) || 0,
    }));

    const svc = await rows<{ service: string; sdk: string }>(
      db,
      `SELECT ServiceName AS service, any(SpanAttributes['telemetry.sdk.version']) AS sdk
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 24 HOUR
       GROUP BY service`,
      tenant,
    );
    // Context-drop detection isn't instrumented yet → 0 drops, but the service inventory is real.
    const contextDrops: ContextDropsByService[] = svc.map((r) => ({
      service: r.service || "unknown",
      sdkVersion: r.sdk || "unknown",
      drops24h: 0,
    }));

    const cal = await rows<{ date: string; est: string; recon: string }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS date,
              sum(EstimatedCost) AS est,
              sumIf(EstimatedCost, CostSource = 'reconciled') AS recon
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 14 DAY
       GROUP BY date ORDER BY date`,
      tenant,
    );
    const calibration: CalibrationDay[] = cal.map((r) => ({
      date: r.date,
      estimatedMicroUsd: micro(r.est),
      reconciledMicroUsd: micro(r.recon),
    }));

    // No stratified-sampling metadata in otel_spans yet → leave empty (the page renders no rows).
    const sampling: SampleByStratum[] = [];

    return {
      overall: {
        attributionRate: totalEvents > 0 ? attributed / totalEvents : 1,
        contextDropCount24h: 0,
        estimateCalibration: 0,
        effectiveSampleRate,
      },
      attribution,
      contextDrops,
      calibration,
      sampling,
    };
  });
}

// --- Agents (summaries + expensive runs) --------------------------------------------------------

interface RunAgg {
  runId: string;
  agent: string;
  cost: string;
  steps: string;
  maxStatus: string;
  tsEpoch: string;
}

// Order-of-magnitude histogram bucket (micro-USD) → 10 buckets, cheap → expensive.
function costBucket(costMicro: number): number {
  if (costMicro <= 0) return 0;
  return Math.min(9, Math.max(0, Math.floor(Math.log10(costMicro))));
}

interface SpanRowRaw {
  runId: string;
  spanId: string;
  parentSpanId: string;
  name: string;
  cost: string;
  durNs: string;
  status: string;
}

function toRunSpan(s: SpanRowRaw): RunSpan {
  return {
    spanId: s.spanId,
    parentSpanId: s.parentSpanId || null,
    name: s.name,
    costMicroUsd: micro(s.cost),
    durationMs: Math.round((parseInt(s.durNs, 10) || 0) / 1e6),
    status: parseInt(s.status, 10) === 2 ? "error" : "ok",
  };
}

// Fetch ordered spans for the given trace ids, grouped by trace id (a plain Record — avoids Map +
// for-of, which behaved unreliably under Next's bundling for this query path).
async function fetchSpansFor(
  db: ClickHouseClient,
  tenant: string,
  runIds: string[],
): Promise<Record<string, RunSpan[]>> {
  const grouped: Record<string, RunSpan[]> = {};
  // Trace ids are hex strings from ClickHouse itself; whitelist-sanitize defensively and inline the
  // IN list (parameterised array/list binding mis-serializes under Next's bundled @clickhouse/client).
  const safeIds = runIds.map((id) => id.replace(/[^0-9a-zA-Z]/g, "")).filter((id) => id.length > 0);
  if (safeIds.length === 0) return grouped;
  const inList = safeIds.map((id) => `'${id}'`).join(",");
  const sql = `SELECT TraceId AS runId, SpanId AS spanId, ParentSpanId AS parentSpanId,
            SpanName AS name, EstimatedCost AS cost, DurationNs AS durNs, StatusCode AS status
     FROM otel_spans
     WHERE TenantId = {tenant:String} AND TraceId IN (${inList})
     ORDER BY AgentStepIndex, Timestamp`;
  const spanRows = await rows<SpanRowRaw>(db, sql, tenant);
  spanRows.forEach((s) => {
    (grouped[s.runId] ??= []).push(toRunSpan(s));
  });
  return grouped;
}

function whyExpensive(spans: RunSpan[], total: number): string {
  if (spans.length === 0 || total <= 0) return "No cost breakdown available for this run.";
  const top = [...spans].sort((a, b) => b.costMicroUsd - a.costMicroUsd)[0];
  const pct = Math.round((top.costMicroUsd / total) * 100);
  return `${pct}% of run cost concentrated in ${top.name} across ${spans.length} steps.`;
}

function buildRun(agg: RunAgg, spans: RunSpan[], agentMedian: number): AgentRun {
  const total = micro(agg.cost);
  return {
    runId: agg.runId,
    agent: agg.agent || "untagged",
    totalCostMicroUsd: total,
    multipleOfMedian: agentMedian > 0 ? Math.round((total / agentMedian) * 10) / 10 : 1,
    steps: parseInt(agg.steps, 10) || spans.length,
    // Only success/failed are inferable from OTel StatusCode (2 = error); abandoned isn't tracked.
    outcome: parseInt(agg.maxStatus, 10) === 2 ? "failed" : "success",
    whyExpensive: whyExpensive(spans, total),
    spans,
  };
}

/** Per-agent summaries + the top expensive runs (with span trees), built from otel_spans. */
export async function queryAgents(): Promise<{ agents: AgentSummary[]; runs: AgentRun[] } | null> {
  return tryLive(async (db, tenant) => {
    const aggs = await rows<RunAgg>(
      db,
      `SELECT TraceId AS runId, any(FeatureTag) AS agent, sum(EstimatedCost) AS cost,
              count() AS steps, max(StatusCode) AS maxStatus, toString(toUnixTimestamp(max(Timestamp))) AS tsEpoch
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != ''
       GROUP BY TraceId`,
      tenant,
    );
    if (aggs.length === 0) return { agents: [], runs: [] };

    // Group runs by agent to derive summaries and per-agent medians.
    const byAgent = new Map<string, RunAgg[]>();
    for (const a of aggs) {
      const list = byAgent.get(a.agent) ?? [];
      list.push(a);
      byAgent.set(a.agent, list);
    }
    const nowEpoch = Math.floor(Date.now() / 1000);
    const dayAgo = nowEpoch - 24 * 3600;
    const agentMedian = new Map<string, number>();

    const agents: AgentSummary[] = [...byAgent.entries()].map(([name, list]) => {
      const costs = list.map((r) => micro(r.cost));
      agentMedian.set(name, median(costs));
      const last24 = list.filter((r) => (parseInt(r.tsEpoch, 10) || 0) >= dayAgo);
      const distribution = new Array(10).fill(0);
      for (const c of costs) distribution[costBucket(c)]++;
      return {
        name,
        runsPerDay: last24.length,
        costPerDayMicroUsd: last24.reduce((s, r) => s + micro(r.cost), 0),
        p50MicroUsd: quantile(costs, 0.5),
        p99MicroUsd: quantile(costs, 0.99),
        distribution,
      };
    });
    agents.sort((a, b) => b.costPerDayMicroUsd - a.costPerDayMicroUsd);

    // Top expensive runs across all agents (with span trees) for the run list / drill-down.
    const topAggs = [...aggs].sort((a, b) => micro(b.cost) - micro(a.cost)).slice(0, 12);
    const spansByRun = await fetchSpansFor(db, tenant, topAggs.map((a) => a.runId));
    const runs = topAggs.map((a) =>
      buildRun(a, spansByRun[a.runId] ?? [], agentMedian.get(a.agent) ?? 0),
    );
    return { agents, runs };
  });
}

/** A single run with its span tree, for the drill-down page. Null when the run isn't in ClickHouse. */
export async function queryAgentRun(runId: string): Promise<AgentRun | null> {
  return tryLive(async (db, tenant) => {
    const aggs = await rowsP<RunAgg>(
      db,
      `SELECT TraceId AS runId, any(FeatureTag) AS agent, sum(EstimatedCost) AS cost,
              count() AS steps, max(StatusCode) AS maxStatus, toString(toUnixTimestamp(max(Timestamp))) AS tsEpoch
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND TraceId = {runId:String}
       GROUP BY TraceId`,
      { tenant, runId },
    );
    const agg = aggs[0];
    if (!agg) return null;

    const peers = await rowsP<{ cost: string }>(
      db,
      `SELECT sum(EstimatedCost) AS cost FROM otel_spans
       WHERE TenantId = {tenant:String} AND FeatureTag = {agent:String}
         AND Timestamp >= now() - INTERVAL 30 DAY
       GROUP BY TraceId`,
      { tenant, agent: agg.agent },
    );
    const agentMedian = median(peers.map((p) => micro(p.cost)));
    const spans = (await fetchSpansFor(db, tenant, [runId]))[runId] ?? [];
    return buildRun(agg, spans, agentMedian);
  });
}

// Connector activity (CTO-63/68): which supported cost/revenue sources are actually producing data.
// Cost sources are read off otel_spans cost layers; revenue sources off business_events.Source. The
// result is keyed by connector id so applyActivity() can mark each catalog entry connected/available.
export async function queryConnectorActivity(): Promise<ConnectorActivity | null> {
  return tryLive(async (db, tenant) => {
    const records: Record<string, number> = {};
    const lastAt: Record<string, string> = {};

    // Cost layers from telemetry. Map each layer back to the connector that feeds it.
    const layerToId = new Map<Layer, string>();
    for (const c of CONNECTORS) {
      if (c.liveKey.kind === "cost-layer") layerToId.set(c.liveKey.layer, c.id);
    }
    const costRows = await rows<{ layer: Layer; n: string; last: string | null }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, count() AS n, toString(max(Timestamp)) AS last
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY
       GROUP BY layer`,
      tenant,
    );
    for (const r of costRows) {
      const id = layerToId.get(r.layer);
      if (!id) continue;
      const n = parseInt(r.n, 10) || 0;
      if (n <= 0) continue;
      records[id] = (records[id] ?? 0) + n;
      if (r.last && (!lastAt[id] || r.last > lastAt[id])) lastAt[id] = r.last;
    }

    // Revenue/CDP sources from business_events. Source value matches the connector id.
    const knownSources = new Set(
      CONNECTORS.filter((c) => c.liveKey.kind === "revenue-source").map((c) => c.id),
    );
    const revRows = await rows<{ src: string; n: string; last: string | null }>(
      db,
      `SELECT lower(Source) AS src, count() AS n, toString(max(OccurredAt)) AS last
       FROM business_events
       WHERE TenantId = {tenant:String}
       GROUP BY src`,
      tenant,
    );
    for (const r of revRows) {
      if (!knownSources.has(r.src)) continue;
      const n = parseInt(r.n, 10) || 0;
      if (n <= 0) continue;
      records[r.src] = (records[r.src] ?? 0) + n;
      if (r.last) lastAt[r.src] = r.last;
    }

    return { records, lastAt };
  });
}
