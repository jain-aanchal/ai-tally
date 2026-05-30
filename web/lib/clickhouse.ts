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
