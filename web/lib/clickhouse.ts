// SPDX-License-Identifier: Apache-2.0
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
import {
  type AttributionFilters,
  type AttributionReport,
  buildProviderRow,
  emptyReport,
} from "./attribution";

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
         SELECT TraceId AS runId, any(ServiceName) AS agent, sum(EstimatedCost) AS cost
         FROM otel_spans
         WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY
           AND ServiceName != '' AND ServiceName != 'unknown'
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

export async function queryCostSeries(filter?: { tag?: string }): Promise<CostSeries | null> {
  return tryLive(async (db, tenant) => {
    const tag = filter?.tag ?? "";
    const tagClause = tag ? "AND FeatureTag = {tag:String}" : "";
    const out = await rowsP<{ day: string; layer: Layer; cost: string }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS day, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 14 DAY ${tagClause}
       GROUP BY day, layer
       ORDER BY day`,
      { tenant, tag },
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

export async function queryFeatureCostRows(filter?: { tag?: string }): Promise<FeatureCostRow[] | null> {
  return tryLive(async (db, tenant) => {
    const tag = filter?.tag ?? "";
    const tagClause = tag ? "AND FeatureTag = {tag:String}" : "";
    const out = await rowsP<{ feature: string; layer: Layer; cost: string }>(
      db,
      `SELECT FeatureTag AS feature, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != '' ${tagClause}
       GROUP BY feature, layer`,
      { tenant, tag },
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

    // CTO-118: ContextDroppedMessages now a typed column (default 0 on legacy rows). Drops count
    // is per-service over 24h. We also pull `total_spans` so the page can distinguish a clean
    // "0 drops in green" (service active, no drops) from "no data this week" (service inactive).
    const svc = await rows<{ service: string; sdk: string; drops: string; spans: string }>(
      db,
      `SELECT ServiceName AS service,
              any(SpanAttributes['telemetry.sdk.version']) AS sdk,
              countIf(ContextDroppedMessages > 0) AS drops,
              count() AS spans
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 24 HOUR
       GROUP BY service`,
      tenant,
    );
    const contextDrops: ContextDropsByService[] = svc.map((r) => ({
      service: r.service || "unknown",
      sdkVersion: r.sdk || "unknown",
      drops24h: parseInt(r.drops, 10) || 0,
      spans24h: parseInt(r.spans, 10) || 0,
    }));
    const contextDropCount24h = contextDrops.reduce((s, c) => s + c.drops24h, 0);

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

    // CTO-119: per-stratum stats from typed columns. The "ci_half" formula is the standard
    // coefficient-of-variation half-width: zCrit × stddev(cost) / mean(cost) / sqrt(n), which
    // assumes cost is approximately log-normal within the stratum. Fine for body (high-volume,
    // similar costs); heroic for tail (rare, expensive) — flagged in CTO-119 as a follow-up
    // where a bootstrap estimator may be warranted. n<30 → null (page renders "—") rather than
    // a meaninglessly wide band.
    const strata = await rows<{ stratum: string; rate: string; n: string; mean: string; std: string }>(
      db,
      `SELECT SamplingStratum AS stratum,
              avg(SamplingRate) AS rate,
              count() AS n,
              avg(EstimatedCost) AS mean,
              stddevPop(EstimatedCost) AS std
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL 30 DAY
         AND SamplingStratum IN ('body', 'mid', 'tail')
       GROUP BY stratum`,
      tenant,
    );
    const Z95 = 1.96;
    const byStratum = new Map(strata.map((r) => {
      const n = parseInt(r.n, 10) || 0;
      const mean = parseFloat(r.mean) || 0;
      const std = parseFloat(r.std) || 0;
      const ci = n >= 30 && mean > 0 ? (Z95 * std) / mean / Math.sqrt(n) : null;
      return [r.stratum, { rate: parseFloat(r.rate) || 0, ci, spans: n }];
    }));
    const sampling: SampleByStratum[] = (["tail", "mid", "body"] as const).map((s) => {
      const row = byStratum.get(s);
      return {
        stratum: s,
        rate: row?.rate ?? 0,
        ciHalfWidthPct: row?.ci ?? null,
        spans: row?.spans ?? 0,
      };
    });

    return {
      overall: {
        attributionRate: totalEvents > 0 ? attributed / totalEvents : 1,
        // CTO-118: real count from typed columns above (sum across services).
        contextDropCount24h,
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
export async function queryAgents(filter?: { tag?: string; run?: string }): Promise<{ agents: AgentSummary[]; runs: AgentRun[] } | null> {
  return tryLive(async (db, tenant) => {
    const tag = filter?.tag ?? "";
    const run = filter?.run ?? "";
    const tagClause = tag ? "AND FeatureTag = {tag:String}" : "";
    const runClause = run ? "AND TraceId = {run:String}" : "";
    // Agent identity comes from ServiceName (e.g. "aider", "vercel-chatbot-demo"),
    // not FeatureTag (which is the workflow-3 dimension — that's the /features view).
    // ?tag= still narrows agents to runs that produced a given feature.
    const aggs = await rowsP<RunAgg>(
      db,
      `SELECT TraceId AS runId,
              any(ServiceName) AS agent,
              sum(EstimatedCost) AS cost,
              count() AS steps,
              max(StatusCode) AS maxStatus,
              toString(toUnixTimestamp(max(Timestamp))) AS tsEpoch
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL 30 DAY
         AND ServiceName != ''
         AND ServiceName != 'unknown'
         ${tagClause}
         ${runClause}
       GROUP BY TraceId`,
      { tenant, tag, run },
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

// --- Tenant integration status (CTO-117) --------------------------------------------------------
//
// The /connectors page used to lean on a hardcoded mockActivity to fill in third-party integration
// state. The real source is the gateway's tenant_integration_runs table, exposed via
// GET /v1/tenant/integrations/status. We fall back to null on any error so the route can paint the
// page with the static mockActivity (same pattern as every other gateway-facing helper here).

/** One row per third-party integration that has had at least one run. */
export interface IntegrationStatusRow {
  connector_id: string;
  last_run_at: string;
  last_run_status: "success" | "partial" | "failed";
  last_run_event_count: number;
  last_run_error_message: string | null;
  total_events_24h: number;
  total_events_7d: number;
}

/**
 * Fetch the caller's per-tenant third-party integration status from the gateway. Returns null on
 * any error (gateway unreachable, non-2xx, parse failure) so the route can fall back to the static
 * mockActivity catalog rather than blanking the page.
 */
export async function queryIntegrationStatus(): Promise<IntegrationStatusRow[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/integrations/status`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      console.warn(`[integrations] /v1/tenant/integrations/status HTTP ${res.status}; falling back`);
      return null;
    }
    const body = (await res.json()) as { integrations?: IntegrationStatusRow[] };
    return Array.isArray(body.integrations) ? body.integrations : [];
  } catch (err) {
    console.warn("[integrations] gateway unreachable:", (err as Error).message);
    return null;
  }
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

// --- Attribution (Workflow 4) --------------------------------------------------------------------

/**
 * $/conversion per provider, joined from otel_spans (cost) ⋈ business_events
 * (outcomes) on UserIdHash. The chatbot demo's lib/tally.ts derives one stable
 * UserIdHash per session — when a session converts, its events share that
 * hash, so the join is direct.
 *
 * Filters are URL-driven (?tag=, ?provider=, ?outcome=) — see lib/attribution.ts.
 * Returns null on any ClickHouse error so the API can fall back to the mock
 * report (CI / fresh-clone friendliness).
 */
export async function queryAttribution(
  filters: AttributionFilters,
): Promise<AttributionReport | null> {
  return tryLive(async (db, tenant) => {
    const outcomeName = filters.outcome ?? "conversion";
    const tagSql = filters.tag ? `AND s.FeatureTag = {tag:String}` : "";
    // CTO-106: prefer the typed GenAiSystem column (the real shape post-CTO-106),
    // fall back to the legacy SpanAttributes['chatbot.real_provider'] for
    // historical rows emitted before the workaround was retired. The
    // SpanAttributes fallback is for historical rows before CTO-106 retired
    // the workaround and can be removed once the 30-day window has rolled.
    const providerExpr =
      `coalesce(nullIf(s.SpanAttributes['chatbot.real_provider'], ''), nullIf(s.GenAiSystem, ''), 'unknown')`;
    const providerSql = filters.provider
      ? `AND ${providerExpr} = {provider:String}`
      : "";

    // sessions per provider (distinct session ids per real provider).
    const sessionsRows = await rowsP<{ provider: string; sessions: string; cost: string }>(
      db,
      `SELECT
         ${providerExpr} AS provider,
         uniqExact(s.SessionId) AS sessions,
         sum(s.EstimatedCost) AS cost
       FROM otel_spans s
       WHERE s.TenantId = {tenant:String}
         AND s.Timestamp >= now() - INTERVAL 30 DAY
         ${tagSql}
         ${providerSql}
       GROUP BY provider`,
      { tenant, tag: filters.tag ?? "", provider: filters.provider ?? "" },
    );

    // Conversions per provider: a business_event whose UserIdHash matches a
    // span's UserIdHash (the demo derives both from sessionId, so 1:1 on the join).
    const conversionRows = await rowsP<{ provider: string; conversions: string }>(
      db,
      `SELECT
         ${providerExpr} AS provider,
         uniqExact(b.BusinessEventId) AS conversions
       FROM business_events b
       INNER JOIN otel_spans s ON s.UserIdHash = b.UserIdHash AND s.TenantId = b.TenantId
       WHERE b.TenantId = {tenant:String}
         AND b.EventName = {outcome:String}
         AND b.OccurredAt >= now() - INTERVAL 30 DAY
         ${tagSql}
         ${providerSql}
       GROUP BY provider`,
      {
        tenant,
        outcome: outcomeName,
        tag: filters.tag ?? "",
        provider: filters.provider ?? "",
      },
    );

    const convByProvider = new Map<string, number>();
    for (const r of conversionRows) {
      convByProvider.set(r.provider, parseInt(r.conversions, 10) || 0);
    }

    // Revenue per provider (CTO-110): sum (conversion + subscription_renewal) − |refund|, and
    // count distinct paying users. Joined on UserIdHash the same way as conversion counts. The
    // sum is in USD decimal (ClickHouse Decimal arithmetic on Int64 micro), we then convert to
    // integer micro-USD at the boundary like everywhere else.
    const revenueRows = await rowsP<{
      provider: string;
      revenue: string;
      users: string;
    }>(
      db,
      `SELECT
         ${providerExpr} AS provider,
         (sumIf(b.ValueAmountMicro, b.EventName IN ('conversion', 'subscription_renewal'))
            - sumIf(abs(b.ValueAmountMicro), b.EventName = 'refund')) / 1000000 AS revenue,
         uniqExact(b.UserIdHash) AS users
       FROM business_events b
       INNER JOIN otel_spans s ON s.UserIdHash = b.UserIdHash AND s.TenantId = b.TenantId
       WHERE b.TenantId = {tenant:String}
         AND b.Source = 'stripe'
         AND b.OccurredAt >= now() - INTERVAL 30 DAY
         AND b.UserIdHash != ''
         ${tagSql}
         ${providerSql}
       GROUP BY provider`,
      { tenant, tag: filters.tag ?? "", provider: filters.provider ?? "" },
    );
    const revenueByProvider = new Map<
      string,
      { revenueMicroUsd: number; distinctUsers: number }
    >();
    for (const r of revenueRows) {
      const users = parseInt(r.users, 10) || 0;
      const revenueMicroUsd = micro(r.revenue);
      if (users > 0) {
        revenueByProvider.set(r.provider, { revenueMicroUsd, distinctUsers: users });
      }
    }

    const perProvider = sessionsRows.map((r) => {
      const sessions = parseInt(r.sessions, 10) || 0;
      const conversions = convByProvider.get(r.provider) ?? 0;
      const costMicro = micro(r.cost);
      const revenue = revenueByProvider.get(r.provider) ?? null;
      return buildProviderRow(r.provider, sessions, conversions, costMicro, revenue);
    });
    perProvider.sort((a, b) => b.sessions - a.sessions);

    const totals = {
      sessions: perProvider.reduce((s, p) => s + p.sessions, 0),
      conversions: perProvider.reduce((s, p) => s + p.conversions, 0),
      costMicroUsd: perProvider.reduce((s, p) => s + p.costMicroUsd, 0),
      costPerConversionMicroUsd: null as number | null,
    };
    totals.costPerConversionMicroUsd =
      totals.conversions > 0 ? Math.round(totals.costMicroUsd / totals.conversions) : null;

    if (perProvider.length === 0) return emptyReport(filters);
    return { filters, perProvider, totals, isMock: false };
  });
}

// --- Compare (Workflow 2) — current model from real traffic --------------------------------------
//
// Replaces the "current" half of the hardcoded mock in lib/compare.ts with a live read. Candidates,
// quality scores, and latencies still mock today (those need workflow-5 replay infra). At least the
// "this is what you're running" half stops being a fiction.
//
// CTO-106 retired the chatbot demo's gpt-5-mini pinning workaround: spans now carry the real
// provider/model on the standard gen_ai.* columns (GenAiSystem / GenAiRequestModel /
// GenAiResponseModel). The SpanAttributes['chatbot.real_provider'] / ['chatbot.real_model']
// reads below are a transitional fallback for historical rows emitted before CTO-106 retired
// the workaround and can be removed once the 30-day window has rolled.
// CTO-115: suppress live p95 / error rate when the 7-day window has fewer than this many spans.
// Small samples produce noisy quantiles and error rates we shouldn't display as if real.
export const MIN_SPANS_FOR_LATENCY_ERROR = 50;

export async function queryCurrentModel(): Promise<{
  model: string;
  provider: string;
  monthlyCostMicroUsd: number;
  // null when sampleCount < MIN_SPANS_FOR_LATENCY_ERROR — the route surfaces these as "—" on the
  // page rather than fabricating numbers off a too-small window.
  latencyP95Ms: number | null;
  errorRate: number | null;
  sampleCount: number;
} | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{
      model: string;
      provider: string;
      cost7d: string;
      // ClickHouse can serialize numeric aggregates as either JSON numbers or strings
      // (count() over UInt64 frequently lands as a string). Accept both at the boundary.
      p95Ms: string | number | null;
      errRate: string | number | null;
      sampleCount: string | number;
    }>(
      db,
      // StatusCode is OTel semconv (UInt8): 0=Unset, 1=Ok, 2=Error — so an error span is
      // `StatusCode = 2`. (The ticket suggested HTTP-style `>= 400`; the codebase already
      // uses `=== 2` for OTel error semantics, e.g. agents.ts toRunSpan().)
      // DurationNs is wrapped in `if(... > 0, ..., NULL)` because some insertion paths land
      // 0-ns durations (mid-stream / early-fail) which would otherwise drag the p95 down.
      `SELECT
         coalesce(
           nullIf(any(SpanAttributes['chatbot.real_model']), ''),
           any(GenAiResponseModel),
           any(GenAiRequestModel)
         ) AS model,
         coalesce(
           nullIf(any(SpanAttributes['chatbot.real_provider']), ''),
           any(GenAiSystem)
         ) AS provider,
         sum(EstimatedCost) AS cost7d,
         quantileExact(0.95)(if(DurationNs > 0, DurationNs, NULL)) / 1e6 AS p95Ms,
         countIf(StatusCode = 2) / count() AS errRate,
         count() AS sampleCount
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL 7 DAY
         AND coalesce(GenAiResponseModel, GenAiRequestModel) != ''
       GROUP BY coalesce(GenAiResponseModel, GenAiRequestModel)
       ORDER BY count() DESC
       LIMIT 1`,
      tenant,
    );
    if (out.length === 0 || !out[0].model) return null;
    // Cost over 7 days → linearly projected to a 30-day month. Honest about what this is.
    const monthlyCostMicroUsd = Math.round((micro(out[0].cost7d) * 30) / 7);
    const sampleCount =
      typeof out[0].sampleCount === "number"
        ? out[0].sampleCount
        : parseInt(out[0].sampleCount, 10) || 0;
    const enoughSamples = sampleCount >= MIN_SPANS_FOR_LATENCY_ERROR;
    const p95Raw = out[0].p95Ms;
    const errRaw = out[0].errRate;
    const p95Num =
      p95Raw === null
        ? null
        : typeof p95Raw === "number"
          ? p95Raw
          : parseFloat(p95Raw);
    const errNum =
      errRaw === null
        ? null
        : typeof errRaw === "number"
          ? errRaw
          : parseFloat(errRaw);
    const latencyP95Ms =
      enoughSamples && p95Num !== null && Number.isFinite(p95Num)
        ? Math.round(p95Num)
        : null;
    const errorRate =
      enoughSamples && errNum !== null && Number.isFinite(errNum) ? errNum : null;
    return {
      model: out[0].model,
      provider: out[0].provider || "unknown",
      monthlyCostMicroUsd,
      latencyP95Ms,
      errorRate,
      sampleCount,
    };
  });
}

// --- Replay-backed candidate projection (CTO-113) -----------------------------------------------
//
// The gateway runs cross-provider replay against captured samples and returns per-candidate
// cost / latency / error rate from real call outcomes. Cached for 5 minutes per (tenant, tag)
// because each projection burns real provider spend — we don't want the dashboard re-replaying
// on every page refresh.

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export interface ReplayCandidateRow {
  provider: string;
  model: string;
  projected_monthly_cost_micro_usd: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  error_rate: number;
  samples_replayed: number;
  excluded_budget_count: number;
}

export interface ReplayProjection {
  samples_available: number;
  per_candidate: ReplayCandidateRow[];
  diagnostics: {
    context_fidelity: string;
    replay_cost_micro_usd: number;
  };
}

const REPLAY_CACHE_TTL_MS = 5 * 60 * 1000;
const _replayCache = new Map<string, { at: number; data: ReplayProjection | null }>();

// Default candidate list when the caller doesn't override. Models come from the SDK's expanded
// catalog (CTO-106) — picked to mirror the existing mock so the dashboard's switcher looks the
// same when replay is active.
const DEFAULT_CANDIDATES = [
  { provider: "anthropic", model: "claude-haiku-4-5" },
  { provider: "openai", model: "gpt-5-mini" },
  { provider: "openai", model: "gpt-4o-mini" },
];

/**
 * Fetch real candidate metrics from the gateway's `/v1/replay` endpoint.
 *
 * Returns null when no samples exist (the route can fall back to its rescaled-mock path) or
 * when the gateway is unreachable. Cached for {@link REPLAY_CACHE_TTL_MS} per (tenant, tag).
 */
export async function queryReplayCandidates(
  featureTag?: string,
  candidates: Array<{ provider: string; model: string }> = DEFAULT_CANDIDATES,
): Promise<ReplayProjection | null> {
  const tenant = TENANT;
  const cacheKey = `${tenant}:${featureTag ?? ""}`;
  const cached = _replayCache.get(cacheKey);
  if (cached && Date.now() - cached.at < REPLAY_CACHE_TTL_MS) {
    return cached.data;
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/replay`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": tenant },
      body: JSON.stringify({
        tenant_id: tenant,
        feature_tag: featureTag,
        candidate_models: candidates,
        sample_size: 50,
      }),
      cache: "no-store",
      // Replay is synchronous — 30s is plenty for 50 samples × 3 candidates on the mock client.
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      console.warn(`[replay] /v1/replay HTTP ${res.status}; falling back to mock`);
      _replayCache.set(cacheKey, { at: Date.now(), data: null });
      return null;
    }
    const body = (await res.json()) as ReplayProjection;
    const data = body.samples_available > 0 ? body : null;
    _replayCache.set(cacheKey, { at: Date.now(), data });
    return data;
  } catch (err) {
    console.warn("[replay] gateway unreachable, falling back to mock:", (err as Error).message);
    _replayCache.set(cacheKey, { at: Date.now(), data: null });
    return null;
  }
}

// --- Body-driven what-if estimate (CTO-128) ---------------------------------------------------
//
// /estimate's POST surface lets an operator swap a candidate model AND tighten the system prompt,
// then re-project cost off the captured corpus. Unlike queryReplayCandidates (which is cached and
// multi-candidate), this is a single-candidate, override-bearing, uncached call — each what-if is
// a distinct intent and burns a fresh (cheap, mock-by-default) replay.

export interface ReplayEstimateRequest {
  candidateModel: { provider: string; model: string };
  systemPromptOverride?: string;
  featureTag?: string;
  sampleSize?: number;
}

/**
 * Fetch a single-candidate what-if projection from the gateway's `/v1/replay/estimate` endpoint.
 *
 * Returns null when no samples ground the estimate or when the gateway is unreachable, so the
 * route can apply its honest-null floor rather than fabricate a number.
 */
export async function queryReplayEstimate(
  req: ReplayEstimateRequest,
): Promise<ReplayProjection | null> {
  const tenant = TENANT;
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/replay/estimate`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": tenant },
      body: JSON.stringify({
        tenant_id: tenant,
        feature_tag: req.featureTag,
        candidate_model: req.candidateModel,
        system_prompt_override: req.systemPromptOverride,
        sample_size: req.sampleSize ?? 50,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      console.warn(`[estimate] /v1/replay/estimate HTTP ${res.status}; returning null`);
      return null;
    }
    const body = (await res.json()) as ReplayProjection;
    return body.samples_available > 0 ? body : null;
  } catch (err) {
    console.warn("[estimate] gateway unreachable, returning null:", (err as Error).message);
    return null;
  }
}

// --- Pairwise LLM-judge eval (CTO-114) --------------------------------------------------------
//
// The gateway's /v1/eval runs a frontier judge over the replay outputs and returns per-candidate
// win-rate with a Wilson 95% CI. We cache aggressively (10 minutes) because each pass burns real
// judge spend — the dashboard must not re-judge on every refresh.

export interface EvalCandidateRow {
  provider: string;
  model: string;
  samples_judged: number;
  current_wins: number;
  candidate_wins: number;
  ties: number;
  errors: number;
  win_rate: number;
  win_rate_ci_lo: number;
  win_rate_ci_hi: number;
  judge_cost_micro_usd: number;
}

export interface EvalProjection {
  samples_available: number;
  per_candidate: EvalCandidateRow[];
  diagnostics: {
    judge_model: string;
    rubric_version: string;
    judge_cost_micro_usd: number;
  };
}

const EVAL_CACHE_TTL_MS = 10 * 60 * 1000;
const _evalCache = new Map<string, { at: number; data: EvalProjection | null }>();

/**
 * Fetch real pairwise-LLM-judge win-rates from the gateway's `/v1/eval` endpoint.
 *
 * Returns null when no eval has run for this tenant yet (no replay corpus, or eval opted-out),
 * or when the gateway is unreachable. The `/api/compare` route honors that null by surfacing
 * the per-candidate `qualityScore` as `null` (rendered "—") rather than fabricating a number.
 * Cached for {@link EVAL_CACHE_TTL_MS} per (tenant, tag).
 */
export async function queryEvalCandidates(
  featureTag?: string,
  candidates: Array<{ provider: string; model: string }> = DEFAULT_CANDIDATES,
): Promise<EvalProjection | null> {
  const tenant = TENANT;
  const cacheKey = `${tenant}:${featureTag ?? ""}`;
  const cached = _evalCache.get(cacheKey);
  if (cached && Date.now() - cached.at < EVAL_CACHE_TTL_MS) {
    return cached.data;
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/eval`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": tenant },
      body: JSON.stringify({
        tenant_id: tenant,
        feature_tag: featureTag,
        candidate_models: candidates,
        sample_size: 50,
      }),
      cache: "no-store",
      // Eval is synchronous — judge calls are slower than replay calls. 10-minute timeout
      // matches the gateway-side allowance.
      signal: AbortSignal.timeout(600_000),
    });
    if (!res.ok) {
      console.warn(`[eval] /v1/eval HTTP ${res.status}; falling back to null`);
      _evalCache.set(cacheKey, { at: Date.now(), data: null });
      return null;
    }
    const body = (await res.json()) as EvalProjection;
    const data = body.samples_available > 0 ? body : null;
    _evalCache.set(cacheKey, { at: Date.now(), data });
    return data;
  } catch (err) {
    console.warn("[eval] gateway unreachable, qualityScore will be null:", (err as Error).message);
    _evalCache.set(cacheKey, { at: Date.now(), data: null });
    return null;
  }
}
