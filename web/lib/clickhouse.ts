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
import type { CostDayPoint, CostSeries, FeatureCostRow, HiddenCostAlert } from "./cost";
import { LAYERS, type Layer } from "./cost";
import type { AttributionDiagnostics, FeatureEconomics } from "./features";
import type {
  AccountCostRow,
  AccountCosts,
  AccountDetail,
  AccountTrendPoint,
  DirectLayer,
  DirectSpendByLayer,
  ExcludedInfraCost,
} from "./accounts";
import {
  DIRECT_LAYERS,
  MAX_ACCOUNT_TOP_FEATURES,
  MAX_ACCOUNT_TOP_RUNS,
  UNATTRIBUTED_ACCOUNT,
  emptyAccountRow,
  totalDirect,
  zeroDirectLayers,
} from "./accounts";
import type {
  AccountStitchConflict,
  AccountStitching,
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
import type { GuardrailMode, GuardrailRule, GuardrailScopeKind } from "./guardrails";
import {
  REFUND_VALUE_TYPE,
  positiveValueTypes,
  queryRevenuePolicy,
  revenueSourceFilter,
} from "./revenueSources";
import {
  accountRevenueReport,
  accountRevenueSql,
  type AccountRevenueReport,
  type AccountRevenueSqlRow,
} from "./accountRevenue";

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

// Map a gen_ai operation to a cost layer. LLM-family spans come from the SDK; `compute` (CTO-143)
// and `egress` (CTO-144) spans are synthetic daily rows the cloud-billing connectors land so the
// Compute and Egress layers populate.
const LAYER_CASE =
  "multiIf(GenAiOperation = 'tool', 'tools', GenAiOperation = 'embeddings', 'embeddings', GenAiOperation = 'vector', 'vector', GenAiOperation = 'compute', 'compute', GenAiOperation = 'egress', 'egress', 'llm')";

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
       WHERE TenantId = {tenant:String} AND Timestamp >= toDate(now()) - INTERVAL 29 DAY`,
      tenant,
    );
    const byLayerRows = await rows<{ layer: Layer; cost: string }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= toDate(now()) - INTERVAL 29 DAY
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
           AND GenAiOperation NOT IN ('compute', 'egress')
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

// The cost chart spans `toDate(now()) - INTERVAL 29 DAY` through today inclusive, i.e. 30 calendar
// days. The window boundary stays this calendar-aligned form (not a rolling `now() - INTERVAL 30
// DAY`) so /cost and Home agree to the penny; see CTO-203.
const COST_SERIES_WINDOW_DAYS = 30;

export async function queryCostSeries(filter?: { tag?: string }): Promise<CostSeries | null> {
  return tryLive(async (db, tenant) => {
    const tag = filter?.tag ?? "";
    const tagClause = tag ? "AND FeatureTag = {tag:String}" : "";
    const out = await rowsP<{ day: string; layer: Layer; cost: string }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS day, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= toDate(now()) - INTERVAL 29 DAY ${tagClause}
       GROUP BY day, layer
       ORDER BY day`,
      { tenant, tag },
    );
    // CTO-203: the day-bucket LIST must come from the window ClickHouse actually reports, not from
    // the Node process clock. The two live in different timezones, and when they straddle midnight
    // the JS-built list shifts a day against the SQL window: the oldest returned row then finds no
    // bucket, the pivot drops it, and that point vanishes from the stacked chart while STILL counting
    // toward the headline total above it, so the chart and its own total silently disagree. This is
    // the exact seam queryAccountDetail (CTO-187) already closes by anchoring its trend on a
    // ClickHouse-sourced window start; we follow that pattern rather than inventing a new mechanism.
    // A dedicated one-row select keeps the boundary available even when `out` is empty (a tenant with
    // no spend in the window still renders 30 zero days).
    const boundary = await rowsP<{ windowStart: string }>(
      db,
      `SELECT toString(toDate(now()) - INTERVAL 29 DAY) AS windowStart`,
      {},
    );
    const windowStart = boundary[0].windowStart;
    // Pivot into one CostDayPoint per calendar day (fill gaps with zero layers). Both ends of the
    // list now come from the same ClickHouse clock as the window predicate above.
    const byDay = new Map<string, CostDayPoint>();
    for (const iso of isoDaysFrom(windowStart, COST_SERIES_WINDOW_DAYS)) {
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
       WHERE TenantId = {tenant:String} AND Timestamp >= toDate(now()) - INTERVAL 29 DAY AND FeatureTag != '' ${tagClause}
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

// --- Settled daily spend, the forecast baseline (CTO-207, F3) -----------------------------------
//
// Cost is NOT complete when a day ends, and a run-rate that pretends otherwise is wrong in the one
// direction that flatters the customer.
//
// Two sources arrive late:
//   1. Connector-sourced `compute` (CTO-143) and `egress` (CTO-144) land as synthetic daily rows
//      from a cloud-billing pull, and the cloud bill itself lags by hours. On the demo tenant those
//      two layers are roughly 46 percent of spend, so a day read at midnight can carry half of what
//      it will eventually carry, and read again at noon it has roughly doubled.
//   2. Reconciliation (`reconciliation_runs`) replaces estimated cost with invoiced cost afterwards.
//
// Project the month from a window whose last day is half-reported and you systematically
// UNDERESTIMATE month-end. So the baseline has to exclude days that are not finished yet, and it
// has to say which days it used: a forecast that cannot state its input window is not auditable.
//
// THE SETTLED-DAY RULE (the one this file implements)
//
//   A day D is settled when BOTH hold:
//     (a) D is strictly before today as ClickHouse reports today. Today is still accruing by
//         definition, whatever its sources have done.
//     (b) every connector-backed layer this tenant actually uses has landed at least one row for D.
//         "Actually uses" is measured over the same window: a tenant with no cloud connector waits
//         on nothing, a tenant with compute only waits on compute.
//
// Why landed rows and not `tenant_compute_config.last_run_at` / `queryReconcilerLastRun`: those
// live in Postgres behind the gateway, one HTTP hop and one more clock away, and they answer
// "did a run finish?" rather than "did the day D we are about to divide by actually arrive?" A run
// can finish having written nothing for D. The landed row IS the completion signal for that day,
// it is per-day rather than per-connector, and it comes back from the same read as the money, so
// there is no window where the two disagree. `last_run_at` remains the right signal for the
// connector health card, which is a different question.
//
// The fallback the ticket allows (a fixed grace period) is what condition (a) degenerates to when a
// tenant has no connector layers at all: withhold the day in progress, trust every completed day.
// A fixed N-hour grace on top of that would be a guess about someone else's billing pipeline; the
// evidence is right there in the table, so we read it instead of guessing.
//
// Known limit, stated rather than papered over: a genuinely idle day with no connector row is
// indistinguishable from a day the connector has not reached yet, so we withhold it. That errs
// toward a smaller, truer baseline, which is the honest direction.
//
// Window: the calendar-aligned `toDate(now()) - INTERVAL 29 DAY` boundary the cost surfaces share,
// widened to reach the start of the calendar month so month-to-date is always fully covered (on the
// 31st a plain 30-day window would clip the 1st). A rolling `now() - INTERVAL 30 DAY` drifts by the
// second and clips a partial day, which is why nothing here uses one.
//
// Clocks: every date in the result — window start, today, the day list, the day count — comes from
// ClickHouse. None of it is generated from the Node process clock. Those are two clocks in two
// timezones, and when they straddle midnight the JS-built list is shifted a day against the SQL
// window, so the oldest day silently has no slot to land in while still counting toward the totals.
// This is the seam CTO-203 fixed in `queryCostSeries`; `queryAccountDetail` does it correctly, and
// so does this.

/** Trailing history for the weekday profile, on the shared calendar-aligned boundary. */
const SETTLED_TRAILING_DAYS = 30;

/** The layers that arrive from a lagging cloud-billing pull rather than from live SDK spans. */
const CONNECTOR_LAYERS = ["compute", "egress"] as const;
type ConnectorLayer = (typeof CONNECTOR_LAYERS)[number];

/**
 * Optional slice of spend to forecast, matching the budget scopes in CTO-205
 * (`tenant` / `feature` / `model` / `layer`). Settlement itself is always judged on the tenant's
 * WHOLE data, never on the slice: whether the cloud bill for Tuesday has landed is a fact about the
 * pipeline, not about the feature you happen to be looking at, and a slice that emitted nothing on
 * Tuesday must not read as "Tuesday is unsettled".
 */
export type SpendScope =
  | { kind: "tenant" }
  | { kind: "feature"; value: string }
  | { kind: "model"; value: string }
  | { kind: "layer"; value: Layer };

export interface SettledDayPoint {
  /** ISO yyyy-mm-dd, from ClickHouse. */
  date: string;
  /** 0 = Sunday … 6 = Saturday, UTC. Derived from `date` by arithmetic, not from the Node clock. */
  weekday: number;
  byLayer: SpendByLayer;
  totalMicroUsd: number;
  /** Inside the calendar month being forecast (i.e. month-to-date), as opposed to trailing history. */
  inPeriod: boolean;
  /** Safe to divide by. See the settled-day rule above. */
  settled: boolean;
  /** True for the day in progress: it is today per ClickHouse and still accruing. */
  inProgress: boolean;
  /** Connector layers with no row for this day yet. Empty when settled or when in progress. */
  awaitingLayers: ConnectorLayer[];
}

export interface SettledSpendTotals {
  dayCount: number;
  byLayer: SpendByLayer;
  totalMicroUsd: number;
}

export interface SettledSpendSeries {
  scope: SpendScope;
  /** Oldest day in the window, per ClickHouse. */
  windowStart: string;
  /** Today per ClickHouse: the day in progress, and the newest day in `days`. */
  windowEnd: string;
  /** First day of the calendar month being forecast. */
  periodStart: string;
  /** Every calendar day in the window, oldest → newest, settled and unsettled alike. */
  days: SettledDayPoint[];
  /**
   * Exactly the days a forecast may compute a run-rate from, oldest → newest, so it can print its
   * own input window. Empty means "no settled history": refuse to project rather than print a
   * number (honest-under-uncertainty, and the minimum-history guard in the scope doc).
   */
  baselineDays: string[];
  /** Newest settled day, or null when none is — never a fabricated date. */
  settledThrough: string | null;
  /** Which connector layers this tenant actually uses, i.e. what settlement waits on. */
  connectorLayers: ConnectorLayer[];
  /** `connector-landing` when the rule waited on a connector, `day-complete` when there was none. */
  rule: "connector-landing" | "day-complete";
  /** Window totals over settled days only. This is the baseline. */
  windowSettled: SettledSpendTotals;
  /** Window totals including unsettled days. Diagnostic: the gap is the late-data exposure. */
  windowObserved: SettledSpendTotals;
  /** Month-to-date over settled days only. */
  periodSettled: SettledSpendTotals;
  /** Month-to-date including unsettled days, i.e. what a naive run-rate would divide. */
  periodObserved: SettledSpendTotals;
  /**
   * Newest day carrying reconciled (invoiced) cost, or null when the reconciler has never run for
   * this window. Days after it are still estimates and can move again.
   */
  reconciledThrough: string | null;
}

function emptyTotals(): SettledSpendTotals {
  return { dayCount: 0, byLayer: zeroLayers(), totalMicroUsd: 0 };
}

function addDay(into: SettledSpendTotals, point: SettledDayPoint): void {
  into.dayCount += 1;
  for (const l of LAYERS) into.byLayer[l] += point.byLayer[l];
  into.totalMicroUsd += point.totalMicroUsd;
}

/** SQL predicate + bound value for a scope. The value is always a parameter, never interpolated. */
function scopeFilter(scope: SpendScope): { clause: string; value: string } {
  switch (scope.kind) {
    case "feature":
      return { clause: "AND FeatureTag = {scope:String}", value: scope.value };
    // Response model when the provider returned one (it is the model that actually served the
    // call), request model otherwise. Matches queryCurrentModel's resolution.
    case "model":
      return {
        clause:
          "AND if(GenAiResponseModel != '', GenAiResponseModel, GenAiRequestModel) = {scope:String}",
        value: scope.value,
      };
    case "layer":
      return { clause: `AND ${LAYER_CASE} = {scope:String}`, value: scope.value };
    default:
      return { clause: "", value: "" };
  }
}

/**
 * Daily spend for the current calendar month plus trailing history, split by cost layer, with every
 * day marked settled or not and the settled subset named explicitly (CTO-207).
 *
 * This is the forecast's input, not the forecast: it deliberately projects nothing. CTO-208 builds
 * the weekday-weighted projection on top of `baselineDays`, and CTO-209/210 put it on a page.
 *
 * Returns `null` (via tryLive) when ClickHouse is unreachable, which the caller must not read as
 * "no spend".
 */
export async function querySettledCostSeries(
  scope: SpendScope = { kind: "tenant" },
): Promise<SettledSpendSeries | null> {
  return tryLive(async (db, tenant) => {
    // Bounds first, from ClickHouse's clock, and reused as a bound parameter by the reads below so
    // all three see one window even if the call straddles midnight.
    const bounds = await rowsP<{
      windowStart: string;
      periodStart: string;
      today: string;
      windowDays: number;
    }>(
      db,
      // The CTE aliases are deliberately not the output names: reusing `windowStart` for both
      // makes the SELECT resolve the alias to itself and the query fails to parse.
      `WITH toDate(now()) AS td,
            toStartOfMonth(td) AS ps,
            least(td - INTERVAL ${SETTLED_TRAILING_DAYS - 1} DAY, ps) AS ws
       SELECT toString(ws) AS windowStart,
              toString(ps) AS periodStart,
              toString(td) AS today,
              toUInt32(dateDiff('day', ws, td) + 1) AS windowDays`,
      { tenant },
    );
    const b = bounds[0];
    if (!b) return null;
    const windowDays = Number(b.windowDays) || 0;

    const { clause, value } = scopeFilter(scope);
    const params = { tenant, windowStart: b.windowStart, scope: value };
    const inWindow = `TenantId = {tenant:String} AND Timestamp >= toDate({windowStart:String})`;

    // The money, sliced by the caller's scope.
    const costRows = await rowsP<{ day: string; layer: Layer; cost: string }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS day, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE ${inWindow} ${clause}
       GROUP BY day, layer`,
      params,
    );

    // The settlement evidence, deliberately UNSCOPED (see SpendScope). One row per day: did each
    // connector layer land, and did anything reconcile. LAYER_CASE is reused rather than re-derived
    // so there is only ever one operation→layer mapping in this file.
    const landingRows = await rowsP<{
      day: string;
      compute: string;
      egress: string;
      reconciled: string;
    }>(
      db,
      `SELECT toString(toDate(Timestamp)) AS day,
              countIf(${LAYER_CASE} = 'compute') AS compute,
              countIf(${LAYER_CASE} = 'egress') AS egress,
              countIf(CostSource = 'reconciled') AS reconciled
       FROM otel_spans
       WHERE ${inWindow}
       GROUP BY day`,
      params,
    );

    const landed = new Map<string, Set<ConnectorLayer>>();
    const usesConnector = new Set<ConnectorLayer>();
    let reconciledThrough: string | null = null;
    for (const r of landingRows) {
      const present = new Set<ConnectorLayer>();
      for (const l of CONNECTOR_LAYERS) {
        if ((parseInt(r[l], 10) || 0) > 0) {
          present.add(l);
          usesConnector.add(l);
        }
      }
      landed.set(r.day, present);
      if ((parseInt(r.reconciled, 10) || 0) > 0 && (!reconciledThrough || r.day > reconciledThrough)) {
        reconciledThrough = r.day;
      }
    }
    const connectorLayers = CONNECTOR_LAYERS.filter((l) => usesConnector.has(l));

    // Every calendar day gets a slot, built from the window ClickHouse reported. A day with no rows
    // is a real zero for the scope, not a hole.
    const byDay = new Map<string, SettledDayPoint>();
    for (const iso of isoDaysFrom(b.windowStart, windowDays)) {
      const inProgress = iso === b.today;
      const present = landed.get(iso) ?? new Set<ConnectorLayer>();
      const awaiting = inProgress ? [] : connectorLayers.filter((l) => !present.has(l));
      byDay.set(iso, {
        date: iso,
        weekday: new Date(`${iso}T00:00:00Z`).getUTCDay(),
        byLayer: zeroLayers(),
        totalMicroUsd: 0,
        inPeriod: iso >= b.periodStart,
        settled: !inProgress && awaiting.length === 0,
        inProgress,
        awaitingLayers: awaiting,
      });
    }
    for (const r of costRows) {
      const point = byDay.get(r.day);
      if (point && (LAYERS as readonly string[]).includes(r.layer)) {
        const m = micro(r.cost);
        point.byLayer[r.layer] += m;
        point.totalMicroUsd += m;
      }
    }

    const days = [...byDay.values()];
    const windowSettled = emptyTotals();
    const windowObserved = emptyTotals();
    const periodSettled = emptyTotals();
    const periodObserved = emptyTotals();
    const baselineDays: string[] = [];
    for (const point of days) {
      addDay(windowObserved, point);
      if (point.inPeriod) addDay(periodObserved, point);
      if (!point.settled) continue;
      baselineDays.push(point.date);
      addDay(windowSettled, point);
      if (point.inPeriod) addDay(periodSettled, point);
    }

    return {
      scope,
      windowStart: b.windowStart,
      windowEnd: b.today,
      periodStart: b.periodStart,
      days,
      baselineDays,
      // null, not a stand-in date: "nothing has settled yet" is a thing we know, and a fake
      // boundary would read as a settled day that does not exist.
      settledThrough: baselineDays.length > 0 ? baselineDays[baselineDays.length - 1] : null,
      connectorLayers,
      rule: connectorLayers.length > 0 ? "connector-landing" : "day-complete",
      windowSettled,
      windowObserved,
      periodSettled,
      periodObserved,
      reconciledThrough,
    };
  });
}

// --- Per-customer cost (CTO-187, D1) ------------------------------------------------------------
//
// These read `daily_account_rollup` (CTO-183), not `otel_spans`. AccountIdHash sits nowhere near the
// front of the spans table's ORDER BY, so grouping by it there cannot skip a granule and degrades
// into a full tenant scan; the rollup leads with (TenantId, AccountIdHash, Day) and turns both reads
// below into index range scans. Read db/clickhouse/account_rollups.sql before changing anything
// here, in particular why its sorting key also carries FeatureTag and GenAiOperation: they are in
// the key for correctness, because SummingMergeTree collapses rows sharing the FULL key and would
// otherwise stamp an arbitrary feature and operation onto a merged total, silently destroying the
// per-layer split these queries depend on.
//
// DIRECT COST ONLY. `compute` and `egress` are excluded from every query here, per Decision 2 in
// docs/cost-per-customer-plan.md. They are tenant-level infrastructure that no span carries an
// account for, so putting them on a customer's bill means inventing an allocation rule (workstream
// C, deferred). CTO-189 surfaces the excluded total separately so the page can state it out loud.
// On the current demo tenant it is about 47 percent of spend, which makes quietly folding it in a
// large lie rather than a small one.
//
// Window: the calendar-aligned `toDate(now()) - INTERVAL 29 DAY` that Home and /cost already use, so
// all three surfaces agree. A rolling `now() - INTERVAL 30 DAY` drifts by the second and clips a
// partial day, which would show this tab disagreeing with /cost for a reason nobody can see.

/** Calendar days in the window, inclusive of today. Kept in step with the SQL below. */
const ACCOUNT_WINDOW_DAYS = 30;
const ACCOUNT_WINDOW = "Day >= toDate(now()) - INTERVAL 29 DAY";

// The same window expressed against otel_spans, whose time column is a Timestamp rather than a Day.
// Deliberately the SAME calendar-aligned boundary rather than `now() - INTERVAL 30 DAY`: the run
// list on the detail view sits under the account total, and a rolling boundary would let a run
// appear that the total above it does not count.
const ACCOUNT_SPAN_WINDOW = "Timestamp >= toDate(now()) - INTERVAL 29 DAY";

/** The four directly attributable layers, expressed as an operation filter. See DIRECT_LAYERS. */
const DIRECT_ONLY = "GenAiOperation NOT IN ('compute', 'egress')";

// AccountIdHash is FixedString(64), which pads to width with NUL bytes, and the unattributed bucket
// is stored as the empty string. Selecting the column raw therefore hands JavaScript a string of 64
// NUL characters rather than '', and every `=== ""` check downstream silently fails. Normalise at the
// SQL boundary so a row's account id is either a real 64-char hex hash or a genuinely empty string.
const ACCOUNT_ID = "if(empty(AccountIdHash), '', toString(AccountIdHash))";

/** `count` consecutive ISO dates starting at `startIso` (yyyy-mm-dd), inclusive. UTC arithmetic. */
function isoDaysFrom(startIso: string, count: number): string[] {
  const [y, m, d] = startIso.split("-").map(Number);
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    out.push(new Date(Date.UTC(y, m - 1, d + i)).toISOString().slice(0, 10));
  }
  return out;
}

/**
 * Direct cost per account over the window: layer split, distinct users, span count.
 *
 * Real accounts come back ranked by cost; the unattributed bucket is returned separately and is
 * always present even at zero, because the page's headline honesty valve is the share of spend with
 * no account and it must be statable unconditionally. Returns `null` (via tryLive) when ClickHouse
 * is unreachable so the route can fall back.
 */
export async function queryAccountCosts(): Promise<AccountCosts | null> {
  return tryLive(async (db, tenant) => {
    // Two reads rather than one, and that is forced. Distinct users comes from a `uniq`
    // AggregateFunction state, and merged states cannot be added together: a user who touched both
    // the llm and the tools layer would be counted once per layer if we summed a per-layer
    // uniqMerge. So users and spans are merged at account grain here, and the layer split is a
    // second pass that only ever sums money.
    const totals = await rows<{ account: string; spans: string; users: string }>(
      db,
      `SELECT ${ACCOUNT_ID} AS account,
              sum(SpanCount) AS spans,
              uniqMerge(UserCountState) AS users
       FROM daily_account_rollup
       WHERE TenantId = {tenant:String} AND ${ACCOUNT_WINDOW} AND ${DIRECT_ONLY}
       GROUP BY account`,
      tenant,
    );
    const byLayerRows = await rows<{ account: string; layer: DirectLayer; cost: string }>(
      db,
      `SELECT ${ACCOUNT_ID} AS account, ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM daily_account_rollup
       WHERE TenantId = {tenant:String} AND ${ACCOUNT_WINDOW} AND ${DIRECT_ONLY}
       GROUP BY account, layer`,
      tenant,
    );

    const layers = new Map<string, DirectSpendByLayer>();
    for (const r of byLayerRows) {
      let m = layers.get(r.account);
      if (!m) {
        m = zeroDirectLayers();
        layers.set(r.account, m);
      }
      if ((DIRECT_LAYERS as readonly string[]).includes(r.layer)) m[r.layer] = micro(r.cost);
    }

    const accounts: AccountCostRow[] = [];
    let unattributed = emptyAccountRow(UNATTRIBUTED_ACCOUNT, true);
    for (const t of totals) {
      const byLayer = layers.get(t.account) ?? zeroDirectLayers();
      const row: AccountCostRow = {
        accountIdHash: t.account,
        unattributed: t.account === UNATTRIBUTED_ACCOUNT,
        byLayer,
        // Sum the rounded per-layer figures rather than rounding the account's own total
        // separately, so a row's layers always add up to the number printed beside them.
        directCostMicroUsd: totalDirect(byLayer),
        distinctUsers: parseInt(t.users, 10) || 0,
        spanCount: parseInt(t.spans, 10) || 0,
      };
      if (row.unattributed) unattributed = row;
      else accounts.push(row);
    }
    accounts.sort((a, b) => b.directCostMicroUsd - a.directCostMicroUsd);

    return {
      windowDays: ACCOUNT_WINDOW_DAYS,
      accounts,
      unattributed,
      // Deliberately the sum of the rows as returned, not a separately rounded tenant-level sum, so
      // the headline can never contradict the table beneath it. This is the figure that must
      // reconcile with /cost's direct spend for the same window.
      totalDirectMicroUsd:
        accounts.reduce((s, a) => s + a.directCostMicroUsd, 0) + unattributed.directCostMicroUsd,
    };
  });
}

/**
 * Compute and egress for the window, the exact complement of what {@link queryAccountCosts} counts.
 *
 * This is the other half of the tenant's bill, and the per-customer tab cannot attribute it: these
 * rows land from the cloud billing connectors as tenant-level daily totals with no account on them.
 * The page states the figure so a reader knows how much of their spend the table below leaves out
 * (CTO-189). Read from the same rollup, over the same window, under the negation of DIRECT_ONLY,
 * so direct plus excluded is the tenant total for the window with nothing double counted and
 * nothing dropped.
 *
 * Returns `null` (via tryLive) when ClickHouse is unreachable; the caller must not read that as
 * zero excluded cost, which would be the most flattering possible reading of a failed query.
 */
export async function queryExcludedInfraCost(): Promise<ExcludedInfraCost | null> {
  return tryLive(async (db, tenant) => {
    // Grouped by layer rather than summed flat so the two figures stay separable: compute and
    // egress have very different fixes, and a later drill-down needs them apart. LAYER_CASE is
    // reused rather than reading GenAiOperation directly so the operation-to-layer mapping keeps
    // living in exactly one place.
    const out = await rows<{ layer: string; cost: string }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM daily_account_rollup
       WHERE TenantId = {tenant:String} AND ${ACCOUNT_WINDOW} AND NOT (${DIRECT_ONLY})
       GROUP BY layer`,
      tenant,
    );
    let computeMicroUsd = 0;
    let egressMicroUsd = 0;
    for (const r of out) {
      if (r.layer === "compute") computeMicroUsd = micro(r.cost);
      else if (r.layer === "egress") egressMicroUsd = micro(r.cost);
    }
    return {
      windowDays: ACCOUNT_WINDOW_DAYS,
      computeMicroUsd,
      egressMicroUsd,
      totalMicroUsd: computeMicroUsd + egressMicroUsd,
    };
  });
}

/**
 * One account: layer split, top features, and a day-by-day trend across the window.
 *
 * Pass `''` for the unattributed bucket. Returns `null` when the tenant has no rollup rows at all
 * for this account in the window, i.e. we know nothing about it — that is a genuinely unknown
 * account, not one that cost zero, and the caller should render it as not found rather than as a
 * free customer. An account we HAVE seen but whose spend is entirely compute and egress comes back
 * as a real row with zeroes, which is a different and true statement.
 */
export async function queryAccountDetail(accountIdHash: string): Promise<AccountDetail | null> {
  return tryLive((db, tenant) => accountDetail(db, tenant, accountIdHash));
}

/**
 * What the detail view actually knows about an account (CTO-190, plan D4).
 *
 * {@link queryAccountDetail} collapses two very different facts into one `null`: "ClickHouse is
 * down" and "this tenant has never emitted a span for this account". A page that renders them the
 * same way tells a reader "no spend recorded for this account" while the store is unreachable,
 * which is the page confidently asserting something it cannot know. The three states are kept
 * apart here so the view can say which one it is looking at.
 */
export type AccountDetailResult =
  | { state: "ok"; detail: AccountDetail }
  /** Reachable, and it holds no rollup row for this account in the window. */
  | { state: "unknown" }
  /** ClickHouse could not be read at all, so nothing is known either way. */
  | { state: "unreachable" };

export async function queryAccountDetailResult(
  accountIdHash: string,
): Promise<AccountDetailResult> {
  // Boxed so the two nulls stay distinguishable: tryLive's own null (query threw) is the outer one,
  // and the query's null (no such account) is the inner one. Without the box they are the same
  // value and the caller has to guess.
  const boxed = await tryLive(async (db, tenant) => ({
    detail: await accountDetail(db, tenant, accountIdHash),
  }));
  if (boxed === null) return { state: "unreachable" };
  return boxed.detail === null ? { state: "unknown" } : { state: "ok", detail: boxed.detail };
}

async function accountDetail(
  db: ClickHouseClient,
  tenant: string,
  accountIdHash: string,
): Promise<AccountDetail | null> {
  {
    const params = { tenant, account: accountIdHash };
    // Comparing FixedString(64) to a String parameter works in both directions: ClickHouse pads the
    // literal, so `''` matches the unattributed bucket and a 64-char hex hash matches exactly.
    const scope = `TenantId = {tenant:String} AND AccountIdHash = {account:String} AND ${ACCOUNT_WINDOW}`;

    // `seen` counts rollup rows WITHOUT the direct-only filter, which is what separates "never heard
    // of this account" from "this account's spend is all infrastructure". The two aggregates beside
    // it carry the filter via the -If combinator so this stays one read.
    const totals = await rowsP<{ seen: string; spans: string; users: string; windowStart: string }>(
      db,
      `SELECT count() AS seen,
              sumIf(SpanCount, ${DIRECT_ONLY}) AS spans,
              uniqMergeIf(UserCountState, ${DIRECT_ONLY}) AS users,
              toString(toDate(now()) - INTERVAL 29 DAY) AS windowStart
       FROM daily_account_rollup
       WHERE ${scope}`,
      params,
    );
    const t = totals[0];
    if (!t || (parseInt(t.seen, 10) || 0) === 0) return null;

    const byLayerRows = await rowsP<{ layer: DirectLayer; cost: string }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, sum(EstimatedCost) AS cost
       FROM daily_account_rollup
       WHERE ${scope} AND ${DIRECT_ONLY}
       GROUP BY layer`,
      params,
    );
    const byLayer = zeroDirectLayers();
    for (const r of byLayerRows) {
      if ((DIRECT_LAYERS as readonly string[]).includes(r.layer)) byLayer[r.layer] = micro(r.cost);
    }

    // FeatureTag = '' is untagged traffic, not a feature. It is left out of the top-features list
    // rather than shown as a nameless row, matching queryFeatureCostRows.
    const featureRows = await rowsP<{ feature: string; cost: string; spans: string }>(
      db,
      `SELECT FeatureTag AS feature, sum(EstimatedCost) AS cost, sum(SpanCount) AS spans
       FROM daily_account_rollup
       WHERE ${scope} AND ${DIRECT_ONLY} AND FeatureTag != ''
       GROUP BY feature
       ORDER BY cost DESC
       LIMIT ${MAX_ACCOUNT_TOP_FEATURES}`,
      params,
    );

    const trendRows = await rowsP<{ day: string; cost: string }>(
      db,
      `SELECT toString(Day) AS day, sum(EstimatedCost) AS cost
       FROM daily_account_rollup
       WHERE ${scope} AND ${DIRECT_ONLY}
       GROUP BY day`,
      params,
    );
    // Fill every calendar day so the chart has no invisible gaps: a day with no spans is a real
    // zero for this account, not missing data.
    //
    // The day list is generated from the window start ClickHouse itself reported, NOT from the
    // Node process clock. Those are two different clocks in two different timezones, and when they
    // straddle midnight the JS-generated list is shifted a day against the SQL window: the oldest
    // day in the result set has no slot to land in and is silently dropped from the trend while
    // still counting toward the account's total, so the chart quietly fails to add up to the number
    // printed above it. Deriving both ends from the same clock removes the seam.
    const byDay = new Map<string, AccountTrendPoint>();
    for (const iso of isoDaysFrom(t.windowStart, ACCOUNT_WINDOW_DAYS)) {
      byDay.set(iso, { date: iso, directCostMicroUsd: 0 });
    }
    for (const r of trendRows) {
      const point = byDay.get(r.day);
      if (point) point.directCostMicroUsd = micro(r.cost);
    }

    // Heaviest runs. This is the ONE read here that cannot come from daily_account_rollup: the
    // rollup is grouped to (account, day, feature, operation) and has no trace id in it at all, so
    // run-grain has to come from otel_spans. That is a wider scan than the rollup reads above, but
    // it is bounded by the same tenant + account + 30 day predicate and returns at most
    // MAX_ACCOUNT_TOP_RUNS rows.
    //
    // Grouping is over THIS ACCOUNT'S SPANS only, not over whole traces that happen to touch the
    // account. A trace serving several customers would otherwise report its full cost against each
    // of them, so the same money would appear on several customers' pages. See AccountRunCost.
    const runRows = await rowsP<{
      runId: string;
      agent: string;
      cost: string;
      steps: string;
      maxStatus: string;
    }>(
      db,
      `SELECT TraceId AS runId,
              any(ServiceName) AS agent,
              sum(EstimatedCost) AS cost,
              count() AS steps,
              max(StatusCode) AS maxStatus
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND AccountIdHash = {account:String}
         AND ${ACCOUNT_SPAN_WINDOW} AND ${DIRECT_ONLY}
       GROUP BY TraceId
       ORDER BY cost DESC
       LIMIT ${MAX_ACCOUNT_TOP_RUNS}`,
      params,
    );

    return {
      accountIdHash,
      unattributed: accountIdHash === UNATTRIBUTED_ACCOUNT,
      byLayer,
      directCostMicroUsd: totalDirect(byLayer),
      distinctUsers: parseInt(t.users, 10) || 0,
      spanCount: parseInt(t.spans, 10) || 0,
      topFeatures: featureRows.map((r) => ({
        feature: r.feature,
        directCostMicroUsd: micro(r.cost),
        spanCount: parseInt(r.spans, 10) || 0,
      })),
      trend: [...byDay.values()],
      topRuns: runRows.map((r) => ({
        runId: r.runId,
        agent: r.agent || "untagged",
        accountCostMicroUsd: micro(r.cost),
        steps: parseInt(r.steps, 10) || 0,
        outcome: parseInt(r.maxStatus, 10) === 2 ? ("failed" as const) : ("success" as const),
      })),
    };
  }
}

// --- Hidden-cost alerts (CTO-122) ---------------------------------------------------------------
//
// Real detection over the telemetry we actually have (otel_spans), replacing the canned
// lib/cost.ts `hiddenCostAlerts` on the live path. Two rules fire today; both run per-tenant over
// the last 30 days and only emit above a sane threshold (no fabricated alerts — returns [] when
// nothing qualifies).
//
//   1. Uncosted tool/agent activity — `GenAiOperation = 'tool'` spans with `EstimatedCost = 0`,
//      i.e. tools running without a cost attached. Emitted only above UNCOSTED_TOOL_THRESHOLD.
//   2. High LLM-calls-per-session ratio — features whose avg LLM spans per SessionId exceeds
//      LLM_PER_SESSION_THRESHOLD, a classic retry-loop / fan-out cost smell.
//
// DEFERRED: the "vendor-billed vs estimated" reconciliation rule (alert when a connector's billed
// spend diverges from our estimate) has NO source today — the billing connectors aren't built yet
// (CTO-143/144). It is intentionally omitted rather than faked.
//
// Alerts are ranked by impact = (share of total spend attributable to the offending feature) ×
// (rule confidence) and capped at the top 5.

// >50 uncosted tool spans before we bother the user (below this is noise).
export const UNCOSTED_TOOL_THRESHOLD = 50;
// avg LLM spans per session above this reads as a retry/fan-out smell worth surfacing.
export const LLM_PER_SESSION_THRESHOLD = 8;
// Keep the alert list short and high-signal.
const MAX_HIDDEN_COST_ALERTS = 5;

interface RankedAlert {
  alert: HiddenCostAlert;
  impact: number;
}

/**
 * Detect hidden-cost alerts from `otel_spans` for the current tenant over the last 30 days.
 *
 * Returns the top {@link MAX_HIDDEN_COST_ALERTS} alerts ranked by impact, or `[]` when nothing
 * qualifies (honest-empty — we never fabricate). Returns `null` (via tryLive) when ClickHouse is
 * unreachable so the route can fall back to the canned mock for CI / fresh-clone rendering.
 */
export async function queryHiddenCostAlerts(filter?: { tag?: string }): Promise<HiddenCostAlert[] | null> {
  return tryLive(async (db, tenant) => {
    const tag = filter?.tag ?? "";
    const tagClause = tag ? "AND FeatureTag = {tag:String}" : "";

    // Total tenant spend over the window — the denominator for the impact ranking.
    const totalRows = await rowsP<{ total: string }>(
      db,
      `SELECT sum(EstimatedCost) AS total
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY ${tagClause}`,
      { tenant, tag },
    );
    const totalSpend = micro(totalRows[0]?.total ?? "0");

    // Rule 1: uncosted tool/agent activity. Count tool spans with EstimatedCost = 0 per feature,
    // alongside that feature's total spend (for the impact ranking).
    const uncostedRows = await rowsP<{ feature: string; uncosted: string; featureCost: string }>(
      db,
      `SELECT
         if(FeatureTag != '', FeatureTag, ServiceName) AS feature,
         countIf(GenAiOperation = 'tool' AND EstimatedCost = 0) AS uncosted,
         sum(EstimatedCost) AS featureCost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY ${tagClause}
       GROUP BY feature
       HAVING uncosted > {threshold:UInt32}
       ORDER BY uncosted DESC`,
      { tenant, tag, threshold: UNCOSTED_TOOL_THRESHOLD },
    );

    // Rule 2: high LLM-calls-per-session ratio. Average LLM spans per SessionId, per feature.
    const ratioRows = await rowsP<{ feature: string; callsPerSession: string; featureCost: string }>(
      db,
      `SELECT
         if(FeatureTag != '', FeatureTag, ServiceName) AS feature,
         countIf(${LAYER_CASE} = 'llm') / nullIf(uniqExact(SessionId), 0) AS callsPerSession,
         sum(EstimatedCost) AS featureCost
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY
         AND SessionId != '' ${tagClause}
       GROUP BY feature
       HAVING callsPerSession > {threshold:Float64}
       ORDER BY callsPerSession DESC`,
      { tenant, tag, threshold: LLM_PER_SESSION_THRESHOLD },
    );

    const ranked: RankedAlert[] = [];

    for (const r of uncostedRows) {
      const feature = r.feature || "untagged";
      const uncosted = parseInt(r.uncosted, 10) || 0;
      if (uncosted <= UNCOSTED_TOOL_THRESHOLD) continue;
      const share = totalSpend > 0 ? micro(r.featureCost) / totalSpend : 0;
      // Confidence is high — a zero-cost tool span is an unambiguous instrumentation gap.
      const confidence = 0.9;
      ranked.push({
        impact: share * confidence,
        alert: {
          severity: "warn",
          message:
            `${feature} ran ${uncosted.toLocaleString()} tool calls with no cost attached over the last 30 days. ` +
            `Those calls are uncosted — the all-in spend for this feature is understated.`,
        },
      });
    }

    for (const r of ratioRows) {
      const feature = r.feature || "untagged";
      const ratio = parseFloat(r.callsPerSession);
      if (!Number.isFinite(ratio) || ratio <= LLM_PER_SESSION_THRESHOLD) continue;
      const share = totalSpend > 0 ? micro(r.featureCost) / totalSpend : 0;
      const confidence = 0.7;
      ranked.push({
        impact: share * confidence,
        alert: {
          severity: "warn",
          message:
            `${feature} averaged ${ratio.toFixed(1)} LLM calls per session over the last 30 days ` +
            `(threshold ${LLM_PER_SESSION_THRESHOLD}). Worth checking for a retry loop or unbounded fan-out.`,
        },
      });
    }

    ranked.sort((a, b) => b.impact - a.impact);
    return ranked.slice(0, MAX_HIDDEN_COST_ALERTS).map((r) => r.alert);
  });
}

// --- Features (ROI + attribution diagnostics) ---------------------------------------------------

// Below this many attributed conversions we refuse to print value/payback/attributionRate — the
// numbers would be too noisy to trust. The UI renders these honest nulls as `—`.
const MIN_CONVERSIONS_FOR_ECONOMICS = 5;

// Per-feature value attribution (CTO-124).
//
// Cost side: sum(EstimatedCost)/uniq(UserIdHash) from `otel_spans` over 30d (unchanged).
//
// Value side: each row in `attribution_records` ties one converting `business_events` row
// (BusinessEventId) to the `FeatureTag` of the agent run that last touched it — the per-feature
// normalization that was missing. We join attribution_records (FINAL, to collapse the
// ReplacingMergeTree) → business_events (FINAL) ON (TenantId, BusinessEventId) to pull EventName and
// the converting user, then per feature compute:
//   valuePerUserMicroUsd = sum(ValueAmountMicro) / distinct converting users
//   paybackDays          = costPerUser / (valuePerUser / 30), guarded against div-by-zero
//   attributionRate      = matched users (have an attribution_record) / total users with that event
//   valueEvent           = the most-frequent EventName attributed to the feature (null if none)
// attributionBreakdown is REAL, not derived: `attribution_records.AttributionConfidence` is an enum
// ('direct' | 'session_stitched' | 'identity_graph_stitched'), so we count attributed users by it
// and add `unmatched` = total-users-with-event minus attributed-users. The four sum to the
// per-feature user total. Honest-null floor: fewer than MIN_CONVERSIONS_FOR_ECONOMICS attributed
// conversions ⇒ value/payback/attributionRate are null (rendered `—`), never fabricated.
export async function queryFeatureEconomics(): Promise<FeatureEconomics[] | null> {
  return tryLive(async (db, tenant) => {
    const cost = await rows<{ feature: string; cost: string; users: string }>(
      db,
      `SELECT FeatureTag AS feature, sum(EstimatedCost) AS cost, uniqExact(UserIdHash) AS users
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY AND FeatureTag != ''
       GROUP BY FeatureTag
       ORDER BY cost DESC`,
      tenant,
    );

    // Attributed value + confidence breakdown per feature, last-touch only. `conversions` counts
    // attributed events; `matched_users` the distinct converting users we tied to a feature.
    const attr = await rows<{
      feature: string;
      value_micro: string;
      conversions: string;
      matched_users: string;
      direct_users: string;
      session_users: string;
      identity_users: string;
    }>(
      db,
      `SELECT
         ar.FeatureTag                                                                    AS feature,
         sum(ifNull(ar.ValueAmountMicro, 0))                                              AS value_micro,
         count()                                                                          AS conversions,
         uniqExact(be.UserIdHash)                                                         AS matched_users,
         uniqExactIf(be.UserIdHash, ar.AttributionConfidence = 'direct')                  AS direct_users,
         uniqExactIf(be.UserIdHash, ar.AttributionConfidence = 'session_stitched')        AS session_users,
         uniqExactIf(be.UserIdHash, ar.AttributionConfidence = 'identity_graph_stitched') AS identity_users
       FROM attribution_records AS ar FINAL
       INNER JOIN (
         SELECT TenantId, BusinessEventId, UserIdHash
         FROM business_events FINAL
         WHERE TenantId = {tenant:String}
       ) AS be
         ON ar.TenantId = be.TenantId AND ar.BusinessEventId = be.BusinessEventId
       WHERE ar.TenantId = {tenant:String} AND ar.AttributedTraceTs >= now() - INTERVAL 30 DAY
       GROUP BY feature`,
      tenant,
    );
    const attrByFeature = new Map(attr.map((r) => [r.feature, r]));

    // Dominant value event per feature: most-frequent EventName among attributed conversions.
    const events = await rows<{ feature: string; event: string; n: string }>(
      db,
      `SELECT ar.FeatureTag AS feature, be.EventName AS event, count() AS n
       FROM attribution_records AS ar FINAL
       INNER JOIN (
         SELECT TenantId, BusinessEventId, EventName
         FROM business_events FINAL
         WHERE TenantId = {tenant:String}
       ) AS be
         ON ar.TenantId = be.TenantId AND ar.BusinessEventId = be.BusinessEventId
       WHERE ar.TenantId = {tenant:String}
       GROUP BY feature, event
       ORDER BY n DESC`,
      tenant,
    );
    const valueEventByFeature = new Map<string, string>();
    for (const e of events) {
      if (!valueEventByFeature.has(e.feature)) valueEventByFeature.set(e.feature, e.event);
    }

    return cost.map((r) => {
      const users = Math.max(1, parseInt(r.users, 10) || 1);
      const costPerUserMicroUsd = Math.round(micro(r.cost) / users);
      const a = attrByFeature.get(r.feature);

      const conversions = a ? parseInt(a.conversions, 10) || 0 : 0;
      const matchedUsers = a ? parseInt(a.matched_users, 10) || 0 : 0;
      const direct = a ? parseInt(a.direct_users, 10) || 0 : 0;
      const sessionStitched = a ? parseInt(a.session_users, 10) || 0 : 0;
      const identityGraphStitched = a ? parseInt(a.identity_users, 10) || 0 : 0;

      // Total users with this feature's event = the converting (matched) users; we have no
      // feature-tagged signal for users whose event never attributed, so `unmatched` here is the
      // gap between the union confidence count and the matched user count (≈ 0 in practice). Until a
      // feature-tagged unattributed source exists (CTO-139 reconciler), attributionRate is matched/
      // matched = 1.0 for features with conversions; we keep the field for forward-compat.
      const attributedUsers = direct + sessionStitched + identityGraphStitched;
      const totalUsers = Math.max(matchedUsers, attributedUsers);
      const unmatched = Math.max(0, matchedUsers - attributedUsers);

      const valueEvent = valueEventByFeature.get(r.feature) ?? null;
      const attributionBreakdown = { direct, sessionStitched, identityGraphStitched, unmatched };

      // Honest-null floor: too few conversions to trust the per-user economics.
      if (conversions < MIN_CONVERSIONS_FOR_ECONOMICS || matchedUsers === 0) {
        return {
          feature: r.feature,
          valueEvent,
          costPerUserMicroUsd,
          valuePerUserMicroUsd: null,
          paybackDays: null,
          attributionRate: null,
          attributionBreakdown,
        };
      }

      const valuePerUserMicroUsd = Math.round((parseInt(a!.value_micro, 10) || 0) / matchedUsers);
      // payback = cost / (value per day). Guard div-by-zero: no value ⇒ payback unknowable.
      const valuePerDay = valuePerUserMicroUsd / 30;
      const paybackDays = valuePerDay > 0 ? Math.round(costPerUserMicroUsd / valuePerDay) : null;
      const attributionRate = totalUsers > 0 ? matchedUsers / totalUsers : null;

      return {
        feature: r.feature,
        valueEvent,
        costPerUserMicroUsd,
        valuePerUserMicroUsd,
        paybackDays,
        attributionRate,
        attributionBreakdown,
      };
    });
  });
}

/** One observed business event name + how often it occurred in the window. */
export interface ObservedBusinessEvent {
  name: string;
  count: number;
}

/**
 * Distinct `business_events.EventName` for the current tenant over the last 30 days, most-frequent
 * first — the live source for the /features "configure value event" modal (CTO-140). Returns null
 * when ClickHouse is unreachable so the route can distinguish "infra down" from "no events yet"
 * (an empty array), which drives the honest-empty state in the modal.
 */
export async function queryDistinctBusinessEventNames(): Promise<ObservedBusinessEvent[] | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ name: string; n: string }>(
      db,
      `SELECT EventName AS name, count() AS n
       FROM business_events
       WHERE TenantId = {tenant:String} AND OccurredAt >= now() - INTERVAL 30 DAY AND EventName != ''
       GROUP BY name
       ORDER BY n DESC`,
      tenant,
    );
    return out.map((r) => ({ name: r.name, count: parseInt(r.n, 10) || 0 }));
  });
}

interface ReconciliationRun {
  events_late: number;
  lag_seconds_median: number;
  finished_at: string;
}

/**
 * The single real source for the reconciler's "last run" freshness (CTO-80): the latest
 * reconciliation_runs row (CTO-139) for this tenant, read from the gateway's reconciler run log via
 * GET /v1/tenant/reconciliation/status. Every surface that carries "reconciler last trued-up N min
 * ago" — Features diagnostics, Agents, Compare, Estimate — derives it from THIS function so they all
 * reflect one truth instead of independent hardcoded constants.
 *
 * Returns null when no reconciler run exists yet (`run` is null) — or the gateway is unreachable /
 * non-2xx — so callers can apply honest-null (`—`) rather than fabricate a value.
 */
async function fetchLatestReconciliationRun(): Promise<ReconciliationRun | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/reconciliation/status`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      console.warn(`[reconciliation] /v1/tenant/reconciliation/status HTTP ${res.status}; falling back`);
      return null;
    }
    const body = (await res.json()) as { run?: ReconciliationRun | null };
    // No reconciler run yet → null so callers render the honest "no data" state.
    return body.run ?? null;
  } catch (err) {
    console.warn("[reconciliation] gateway unreachable:", (err as Error).message);
    return null;
  }
}

function minutesSince(finishedAt: string): number {
  return Math.max(0, Math.round((Date.now() - Date.parse(finishedAt)) / 60000));
}

/**
 * Minutes since the reconciler last finished a pass for the current tenant, from the real
 * reconciliation_runs source (see {@link fetchLatestReconciliationRun}). Returns null when the
 * reconciler has never run or the gateway is unavailable — the caller renders `—` (honest-null),
 * NEVER a fabricated constant (CTO-169 / the CTO-80 staleness guard).
 */
export async function queryReconcilerLastRun(): Promise<number | null> {
  const run = await fetchLatestReconciliationRun();
  return run ? minutesSince(run.finished_at) : null;
}

/**
 * Late-arrival diagnostics for the /features attribution card (CTO-139). The gateway returns the
 * latest reconciliation run (event-late count + lag distribution in seconds + finished_at); we
 * convert to the page's units (hours / minutes-ago). Reads the same real source as
 * {@link queryReconcilerLastRun} so the freshness signal agrees across surfaces.
 *
 * Honest-null: when no reconciler run exists yet — or the gateway is unreachable / non-2xx — we
 * return null so the /api/features route falls back to its mock via `?? diagnostics`.
 */
export async function queryAttributionDiagnostics(): Promise<AttributionDiagnostics | null> {
  const run = await fetchLatestReconciliationRun();
  if (!run) return null;
  return {
    lateArrivalEvents7d: run.events_late,
    lateArrivalMedianHours: Math.round((run.lag_seconds_median / 3600) * 10) / 10,
    reconcilerLastRunMinutesAgo: minutesSince(run.finished_at),
  };
}

// --- Account stitching (CTO-184) ----------------------------------------------------------------

/**
 * Normalises `identity_graph` edges into (person, account) pairs.
 *
 * `account_id` is the sixth `IdentityAType` / `IdentityBType` value (CTO-184) and either side of an
 * edge may carry it, so we pick whichever side is the account and take the other as the person.
 * The `!=` on the two booleans is an XOR: it keeps edges where EXACTLY ONE side is an account.
 * Account-to-account edges say nothing about a person and person-to-person edges are the ordinary
 * identity graph, so both are excluded rather than half-interpreted.
 */
const ACCOUNT_PAIRS_CTE = `
  WITH pairs AS (
    SELECT
      if(IdentityAType = 'account_id', IdentityB, IdentityA) AS person_hash,
      if(IdentityAType = 'account_id', IdentityA, IdentityB) AS account_hash
    FROM identity_graph
    WHERE TenantId = {tenant:String}
      AND ((IdentityAType = 'account_id') != (IdentityBType = 'account_id'))
  )`;

/**
 * Account-dimension coverage plus the multi-account conflicts that block attribution (CTO-184).
 *
 * Two things a consumer needs and cannot get anywhere else:
 *
 * 1. **Direct vs stitched.** A directly-tagged account was stamped on the span at emit time
 *    (`otel_spans.AccountIdHash`, CTO-180) and is exactly as trustworthy as the span. A stitched
 *    account was inferred from an `account_id` edge a CRM or CDP connector asserted, and is only
 *    as trustworthy as that connector. They are counted separately so the UI can say which it is
 *    showing rather than blending two very different confidences into one number.
 *
 * 2. **Conflicts.** One user belongs to one account. Where a user is observed against more than
 *    one, we attribute NOTHING for them: no split, no duplication, no first-seen. Duplicating a
 *    user's spend across accounts inflates the tenant total, per-account figures then stop summing
 *    to what /cost reports, and the per-customer surface loses its reconciliation guarantee. The
 *    query returns the conflicting users so the refusal is visible instead of silent, along with
 *    the spend actually held back by it, so a tenant can weigh fixing their CRM.
 *
 * `withheldMicroUsd` counts only spans with an EMPTY `AccountIdHash`. A conflicted user's directly
 * tagged spans are unaffected: they carry their own account and never needed stitching, so
 * counting them here would overstate the damage.
 */
export async function queryAccountStitching(): Promise<AccountStitching | null> {
  return tryLive(async (db, tenant) => {
    const coverage = await rows<{ stitched_accounts: string; stitched_users: string }>(
      db,
      `${ACCOUNT_PAIRS_CTE}
       SELECT uniqExact(account_hash) AS stitched_accounts,
              uniqExact(person_hash)  AS stitched_users
       FROM pairs`,
      tenant,
    );

    const direct = await rows<{ direct_accounts: string }>(
      db,
      `SELECT uniqExact(AccountIdHash) AS direct_accounts
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL 30 DAY
         AND AccountIdHash != ''`,
      tenant,
    );

    const conflicting = await rows<{ person_hash: string; accounts: string[] }>(
      db,
      `${ACCOUNT_PAIRS_CTE}
       SELECT person_hash, arraySort(groupUniqArray(account_hash)) AS accounts
       FROM pairs
       GROUP BY person_hash
       HAVING length(accounts) > 1
       ORDER BY person_hash
       LIMIT 100`,
      tenant,
    );

    // Nothing ambiguous: skip the cost query entirely rather than run an `IN ()` over no users.
    const users = conflicting.map((r) => r.person_hash.replace(/\0+$/, ""));
    const costByUser = new Map<string, { cost: string; spans: string }>();
    if (users.length > 0) {
      const costs = await rowsP<{ user_hash: string; cost: string; spans: string }>(
        db,
        `SELECT UserIdHash AS user_hash, sum(EstimatedCost) AS cost, count() AS spans
         FROM otel_spans
         WHERE TenantId = {tenant:String}
           AND Timestamp >= now() - INTERVAL 30 DAY
           AND AccountIdHash = ''
           AND UserIdHash IN {users:Array(String)}
         GROUP BY user_hash`,
        { tenant, users },
      );
      for (const c of costs) {
        costByUser.set(c.user_hash.replace(/\0+$/, ""), { cost: c.cost, spans: c.spans });
      }
    }

    const conflicts: AccountStitchConflict[] = conflicting.map((r) => {
      const user = r.person_hash.replace(/\0+$/, "");
      const c = costByUser.get(user);
      return {
        userIdHash: user,
        accounts: r.accounts.map((a) => a.replace(/\0+$/, "")),
        withheldMicroUsd: micro(c?.cost),
        spans30d: parseInt(c?.spans ?? "0", 10) || 0,
      };
    });

    return {
      directAccounts: parseInt(direct[0]?.direct_accounts ?? "0", 10) || 0,
      stitchedAccounts: parseInt(coverage[0]?.stitched_accounts ?? "0", 10) || 0,
      stitchedUsers: parseInt(coverage[0]?.stitched_users ?? "0", 10) || 0,
      conflicts,
    };
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

    // CTO-184: account coverage + multi-account conflicts. This runs inside the same tryLive as
    // the rest of the report, so if `identity_graph` is missing the whole report falls back to
    // mock rather than half-rendering, the same failure posture every other section has.
    const accountStitching = await queryAccountStitching();

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
      accountStitching: accountStitching ?? undefined,
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
         AND GenAiOperation NOT IN ('compute', 'egress')
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

// --- Tenant guardrails (CTO-120 / control-plane CTO-116) ----------------------------------------
//
// The /guardrails page used to serve the typed mock from lib/guardrails.ts. The real source is the
// gateway's tenant_guardrails table, exposed via GET /v1/tenant/guardrails. We map the control-plane
// rule shape (rule_id / kind / params / state) onto the web's GuardrailRule and fall back to null on
// any error so the route can paint the page with the static mock (same pattern as every other
// gateway-facing helper here).
//
// Shape mapping (gateway -> web):
//   rule_id                       -> id
//   params.scope_kind ("feature") -> scopeKind (default "agent")
//   params.scope / rule_id        -> scope
//   params.mode OR state          -> mode  (params.mode wins; else shadow/disabled->observe,
//                                            enabled->warn as a safe enforcing default)
//   params.max_cost_micro_usd     -> maxCostMicroUsd
//   params.max_steps              -> maxSteps
// The control plane does not carry fire counts, so wouldHaveFiredThisWeek / runsThisWeek default to
// 0 (those are observability tallies the SDK emits, not config) — honest rather than fabricated.

interface GatewayGuardrailRule {
  rule_id: string;
  kind: string;
  params?: Record<string, unknown> | null;
  state: string;
}

function guardrailIntOrNull(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : parseInt(String(v), 10);
  return Number.isFinite(n) ? n : null;
}

function mapGuardrailRule(r: GatewayGuardrailRule): GuardrailRule {
  const params = r.params ?? {};
  const rawMode = params["mode"];
  const mode: GuardrailMode =
    rawMode === "observe" || rawMode === "warn" || rawMode === "graceful" || rawMode === "hard_stop"
      ? rawMode
      : r.state === "enabled"
        ? "warn"
        : "observe"; // shadow / disabled / unknown -> observe (never alters the agent)
  const scopeKind: GuardrailScopeKind = params["scope_kind"] === "feature" ? "feature" : "agent";
  const scope =
    typeof params["scope"] === "string" && params["scope"] ? (params["scope"] as string) : r.rule_id;
  return {
    id: r.rule_id,
    scopeKind,
    scope,
    mode,
    maxCostMicroUsd: guardrailIntOrNull(params["max_cost_micro_usd"]),
    maxSteps: guardrailIntOrNull(params["max_steps"]),
    // Control plane carries config, not telemetry — fire counts come from the SDK, default 0.
    wouldHaveFiredThisWeek: guardrailIntOrNull(params["would_have_fired_this_week"]) ?? 0,
    runsThisWeek: guardrailIntOrNull(params["runs_this_week"]) ?? 0,
  };
}

// Per-rule trip telemetry (CTO-146). The control plane stores config, not counts — the real
// runsThisWeek / wouldHaveFiredThisWeek come from guardrail-verdict spans the SDK emits. When a
// rule is evaluated on a trace the SDK sets one map attribute per rule:
//   SpanAttributes['gen_ai.guardrail.{rule_id}.verdict'] ∈ {enforced, shadow_observed, passed}
// We count those over the trailing 7d:
//   runsThisWeek           = every verdict present (the rule was evaluated)
//   wouldHaveFiredThisWeek = verdict ∈ {enforced, shadow_observed} (it fired / would have)
// The key is dynamic per rule_id, so we ARRAY JOIN the attribute map and extract the rule_id from
// keys matching `gen_ai.guardrail.%.verdict` — no need to enumerate rule ids in SQL.
export interface GuardrailActivity {
  runsThisWeek: number;
  wouldHaveFiredThisWeek: number;
}

export async function queryGuardrailActivity(): Promise<Map<string, GuardrailActivity> | null> {
  return tryLive(async (db, tenant) => {
    const out = await rows<{ ruleId: string; runs: string; wouldFire: string }>(
      db,
      `SELECT
         extract(key, '^gen_ai\\\\.guardrail\\\\.(.+)\\\\.verdict$') AS ruleId,
         count() AS runs,
         countIf(val IN ('enforced', 'shadow_observed')) AS wouldFire
       FROM otel_spans
       ARRAY JOIN mapKeys(SpanAttributes) AS key, mapValues(SpanAttributes) AS val
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL 7 DAY
         AND key LIKE 'gen_ai.guardrail.%.verdict'
       GROUP BY ruleId`,
      tenant,
    );
    const activity = new Map<string, GuardrailActivity>();
    for (const r of out) {
      if (!r.ruleId) continue;
      activity.set(r.ruleId, {
        runsThisWeek: parseInt(r.runs, 10) || 0,
        wouldHaveFiredThisWeek: parseInt(r.wouldFire, 10) || 0,
      });
    }
    return activity;
  });
}

/**
 * Fetch the caller's per-tenant guardrail rules from the gateway and map them onto the web's
 * GuardrailRule shape. Returns null on any error (gateway unreachable, non-2xx, parse failure) so
 * the route can fall back to the static mock (`?? guardrailRules`) rather than blanking the page.
 *
 * The gateway carries config only, so trip counts are merged in from verdict-span telemetry
 * (queryGuardrailActivity). A rule with no verdict spans keeps runsThisWeek = 0, which the UI
 * renders as `—` (honest null) rather than a fabricated count.
 */
export async function queryGuardrailRules(): Promise<GuardrailRule[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/guardrails`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      console.warn(`[guardrails] /v1/tenant/guardrails HTTP ${res.status}; falling back to mock`);
      return null;
    }
    const body = (await res.json()) as { rules?: GatewayGuardrailRule[] };
    const rules = Array.isArray(body.rules) ? body.rules.map(mapGuardrailRule) : [];
    // Overlay live trip counts from verdict spans. If telemetry is unavailable (null), leave the
    // config-default 0s — never fabricate a count.
    const activity = await queryGuardrailActivity();
    if (activity) {
      for (const rule of rules) {
        const a = activity.get(rule.id);
        if (a) {
          rule.runsThisWeek = a.runsThisWeek;
          rule.wouldHaveFiredThisWeek = a.wouldHaveFiredThisWeek;
        }
      }
    }
    return rules;
  } catch (err) {
    console.warn("[guardrails] gateway unreachable, falling back to mock:", (err as Error).message);
    return null;
  }
}

// --- Feature value-event config (CTO-140) -------------------------------------------------------
//
// Onboarding pins each feature's ROI to a business value event; the config lives in the control
// plane (Postgres), reached via the gateway. The /features route overlays these onto the economics
// rows so a just-configured feature shows its value event even before attribution has run. Returns
// null on any error (gateway unreachable, non-2xx, parse failure) so the route falls back to
// whatever the economics query produced.

export interface FeatureValueEventConfig {
  featureTag: string;
  eventName: string;
}

export async function queryFeatureValueEvents(): Promise<FeatureValueEventConfig[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/feature-value-events`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      console.warn(`[value-events] /v1/tenant/feature-value-events HTTP ${res.status}; falling back`);
      return null;
    }
    const body = (await res.json()) as {
      value_events?: { feature_tag?: string; event_name?: string }[];
    };
    if (!Array.isArray(body.value_events)) return [];
    return body.value_events
      .filter((e): e is { feature_tag: string; event_name: string } =>
        typeof e.feature_tag === "string" && typeof e.event_name === "string",
      )
      .map((e) => ({ featureTag: e.feature_tag, eventName: e.event_name }));
  } catch (err) {
    console.warn("[value-events] gateway unreachable:", (err as Error).message);
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

    // Cost layers from telemetry, resolved back to the connector that produced them.
    //
    // Two connectors can feed the same layer (AWS and GCP both feed `compute`; Vercel and
    // Cloudflare both feed `egress`), so a plain layer→connector map collides and credits every
    // row to whichever connector the catalog lists last. Group by GenAiSystem as well and prefer
    // the connector that claims that system; fall back to the layer's sole connector when a layer
    // has exactly one, so single-connector layers (llm, vector, tools) are unaffected.
    const bySystem = new Map<string, string>();
    const layerOwners = new Map<Layer, string[]>();
    for (const c of CONNECTORS) {
      if (c.liveKey.kind !== "cost-layer") continue;
      const owners = layerOwners.get(c.liveKey.layer) ?? [];
      owners.push(c.id);
      layerOwners.set(c.liveKey.layer, owners);
      for (const sys of c.liveKey.systems ?? []) bySystem.set(`${c.liveKey.layer}:${sys}`, c.id);
    }
    const costRows = await rows<{ layer: Layer; system: string; n: string; last: string | null }>(
      db,
      `SELECT ${LAYER_CASE} AS layer, GenAiSystem AS system, count() AS n,
              toString(max(Timestamp)) AS last
       FROM otel_spans
       WHERE TenantId = {tenant:String} AND Timestamp >= now() - INTERVAL 30 DAY
       GROUP BY layer, system`,
      tenant,
    );
    for (const r of costRows) {
      const owners = layerOwners.get(r.layer) ?? [];
      // Exact system match wins; otherwise only credit a layer that has a single connector. A
      // shared layer with an unrecognised system stays unattributed rather than guessing.
      const id = bySystem.get(`${r.layer}:${r.system}`) ?? (owners.length === 1 ? owners[0] : null);
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
    // CTO-194: which sources/value types count as revenue for this tenant. Resolved from the
    // control plane, and falls back to the defaults (all sources, monetary + mrr, refunds net off)
    // when the tenant has no row or the gateway is unreachable — it never throws, so a gateway
    // outage degrades to the default policy rather than blanking the whole attribution report.
    const policy = await queryRevenuePolicy();
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
         AND s.GenAiOperation NOT IN ('compute', 'egress')
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
         AND s.GenAiOperation NOT IN ('compute', 'egress')
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

    // Revenue per provider (CTO-110, reworked in CTO-194).
    //
    // This used to require `b.Source = 'stripe'` and key off EventName. Both were wrong: `Source`
    // is an unconstrained LowCardinality(String) chosen by whichever connector ingested the row, so
    // any tenant on a non-Stripe biller had 100% of its revenue silently dropped, and EventName is
    // equally freeform. `ValueType` is the real discriminator — a ClickHouse enum of
    // ('monetary'=1,'count'=2,'mrr'=3,'refund'=4) — so we sum the money-typed events and subtract
    // refunds, which net off rather than being ignored. Source is now only ever a per-tenant
    // NARROWING, and absent config means every source counts (see lib/revenueSources.ts).
    //
    // `users` counts only users who actually carry a revenue-typed event; a user whose only event
    // is a 'count' engagement signal must not dilute value/user into a fabricated number.
    //
    // ValueAmountMicro is Nullable(Int64), so `sumIf` over a group with no matching row yields NULL
    // rather than 0 and the NULL then swallows the whole subtraction. The `ifNull(..., 0)` inside
    // each sumIf is what keeps a tenant with zero refunds from reporting NULL revenue.
    const positiveTypes = positiveValueTypes(policy);
    const sourceFilter = revenueSourceFilter(policy, "b");
    const revenueRows = await rowsP<{
      provider: string;
      revenue: string;
      users: string;
    }>(
      db,
      `SELECT
         ${providerExpr} AS provider,
         (sumIf(ifNull(b.ValueAmountMicro, 0), b.ValueType IN {positiveTypes:Array(String)})
            - sumIf(abs(ifNull(b.ValueAmountMicro, 0)), b.ValueType = {refundType:String}))
           / 1000000 AS revenue,
         uniqExactIf(
           b.UserIdHash,
           b.ValueType IN {positiveTypes:Array(String)} OR b.ValueType = {refundType:String}
         ) AS users
       FROM business_events b
       INNER JOIN otel_spans s ON s.UserIdHash = b.UserIdHash AND s.TenantId = b.TenantId
       WHERE b.TenantId = {tenant:String}
         AND b.OccurredAt >= now() - INTERVAL 30 DAY
         AND b.UserIdHash != ''
         ${sourceFilter.sql}
         ${tagSql}
         ${providerSql}
       GROUP BY provider`,
      {
        tenant,
        tag: filters.tag ?? "",
        provider: filters.provider ?? "",
        positiveTypes,
        refundType: REFUND_VALUE_TYPE,
        ...sourceFilter.params,
      },
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

// --- Revenue per account (CTO-196) --------------------------------------------------------------

/**
 * Net revenue per account over the same 30 day window the cost queries use.
 *
 * The SQL and the null-vs-zero rule live in lib/accountRevenue.ts, which documents both; this is
 * only the round trip. Two properties worth restating here because they are easy to break:
 *
 * - The tenant's E1 revenue policy decides what counts, so uploaded revenue and connector revenue
 *   go through one filter. A gateway outage falls back to the default policy rather than blanking
 *   the figure, same as queryAttribution.
 * - The grouping key is the `AccountIdHash` already on the row. Revenue is never re-keyed from a
 *   user here: E2's AccountLinker owns the one user, one account rule and lands ambiguous revenue
 *   unattributed with an AccountConflict finding, and the unattributed bucket is reported rather
 *   than dropped.
 *
 * Returns null on any ClickHouse error, so a caller falls back rather than rendering a zero.
 */
export async function queryAccountRevenue(): Promise<AccountRevenueReport | null> {
  return tryLive(async (db, tenant) => {
    const policy = await queryRevenuePolicy();
    const { sql, params } = accountRevenueSql(policy);
    const rows = await rowsP<AccountRevenueSqlRow>(db, sql, { tenant, ...params });
    return accountRevenueReport(rows);
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
//
// CTO-166: Google/Gemini is a first-class priced provider (CTO-149), so the gemini candidate now
// goes through the SAME /v1/replay + /v1/eval path as anthropic/openai — no provider allowlist.
// It was previously absent here, which meant the gemini-3-flash row never got real replay/eval
// data and stayed stuck on the compare.ts mock fallback. Adding it is the whole fix: the route
// and the gateway clients are already provider-generic.
//
// CTO-171: dropped the retired `openai/gpt-4o-mini` — it is no longer a model we want the Compare
// switcher to surface. Every id below MUST be a current, catalog-priced model (see the SDK's
// `seed_catalog()` in sdk/python/src/tally/pricing.py). This list is hardcoded rather than derived
// from live provider discovery: the gateway discovers its lineup at boot (`app.state.models` via
// `discover_models()`), but it does NOT yet expose that over HTTP — there is no `/v1/models` route.
// Building one is the follow-up; until then, the guard test in clickhouse.test.ts asserts every id
// here is present in a current known-good catalog set, so a retired id can't silently return.
export const DEFAULT_CANDIDATES = [
  { provider: "anthropic", model: "claude-haiku-4-5" },
  { provider: "openai", model: "gpt-5-mini" },
  { provider: "google", model: "gemini-3-flash" },
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
