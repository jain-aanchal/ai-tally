// SPDX-License-Identifier: Apache-2.0
// The "paid-for-nothing" waste detector (CTO-229, W2 of epic CTO-227).
//
// This is the most literal form of AI waste: a run that consumed billable tokens and then produced
// nothing usable -- it ended in a terminal error (failed) or was abandoned. The tenant paid for the
// tokens; there is no result to show for them. Recovering this spend is the highest-confidence
// finding the epic emits, because "billed AND no successful outcome" needs no modelling or
// counterfactual: the dollars were spent, the outcome was empty.
//
// The module is deliberately SELF-CONTAINED (CTO-227 boundary): it owns BOTH its ClickHouse query
// and its pure detector, so it never edits clickhouse.ts, waste.ts, or any shared surface. It reuses
// the exported helpers (`tryLive`, `rowsP`, `micro`) and, critically, the SAME run-outcome derivation
// as `queryAgents` -- a run is failed when `max(StatusCode) = 2` (OTel semconv: 2 = Error), success
// otherwise. We do not invent a second outcome definition.
//
// Honesty posture (CLAUDE.md): money is integer micro-USD throughout; a scope with no wasted spend
// yields no finding (never a fabricated or zero-dollar finding). `recoverableMicroUsd` here is always
// bounded and non-null because the wasted spend is directly observed, not estimated.

import type { WasteFinding } from "@/lib/waste";
import type { DimensionFilters } from "@/lib/filters";
import { tryLive, rowsP, micro } from "@/lib/clickhouse";
import { clampWindowDays } from "@/lib/explore";

/**
 * One aggregated "paid-for-nothing" row for a single scope (a feature tag or an agent/service name).
 * `wastedCost` and `scopeCost` are decimal-USD strings straight from ClickHouse `sum(...)`; the pure
 * detector converts them to integer micro-USD via {@link micro}. Counts arrive as strings too
 * (ClickHouse serializes UInt aggregates as strings in JSONEachRow).
 */
export interface PaidForNothingRow {
  scopeKind: "feature" | "agent";
  scopeValue: string;
  /** Summed EstimatedCost of the wasted (billed-but-empty) runs in this scope. Decimal-USD string. */
  wastedCost: string;
  /**
   * The scope's TRUE total EstimatedCost over the window: the denominator for `shareOfScopeSpend`.
   * For a feature scope this is every run carrying that FeatureTag; for an agent scope it is every run
   * on that ServiceName, INCLUDING the runs that were attributed to a feature for the wasted-cost
   * roll-up (CTO-227 review pass 2). It is NOT the sum of the single-scoped runs. Decimal-USD string.
   */
  scopeCost: string;
  /** Count of wasted runs that ended failed (terminal error StatusCode). */
  failedRuns: string;
  /** Count of wasted runs that ended abandoned. Not derivable from OTel StatusCode today, so 0. */
  abandonedRuns: string;
  /** One example wasted TraceId, for the drill-down. May be empty. */
  exampleTrace: string;
}

/**
 * Pure detector: turn aggregated per-scope rows into {@link WasteFinding}s. No I/O, no clock, no
 * randomness -- unit-tested directly on fixtures.
 *
 * A row becomes a finding only when it has wasted spend (`wastedCost` > 0 in micro-USD). The
 * recoverable is exactly that wasted spend (dollars already paid with no result), and it is always
 * bounded (never null): the money is observed, not modelled. `windowSpendMicroUsd` is the scope's
 * TRUE total spend over the window (see {@link PaidForNothingRow.scopeCost}), so `shareOfScopeSpend`
 * is wasted-over-true-total and neither it nor the reason text overstates the scope (CTO-227 review
 * pass 2): an agent whose wasted spend is 20% of its real total reports 20%, not 100%.
 */
export function detectPaidForNothing(rows: PaidForNothingRow[]): WasteFinding[] {
  const findings: WasteFinding[] = [];
  for (const r of rows) {
    const wasted = micro(r.wastedCost);
    // No wasted spend -> no finding. Never emit a zero-dollar "waste" (CTO-227 honesty).
    if (wasted <= 0) continue;

    const scopeSpend = micro(r.scopeCost);
    const failedRuns = parseInt(r.failedRuns, 10) || 0;
    const abandonedRuns = parseInt(r.abandonedRuns, 10) || 0;
    const wastedRuns = failedRuns + abandonedRuns;
    // Share of the scope's spend that went to nothing. Guard against a zero/again-smaller total
    // (shouldn't happen: wasted is a subset of scope spend) so the percentage stays sane.
    const shareOfScopeSpend =
      scopeSpend > 0 ? Math.round((wasted / scopeSpend) * 100) : 100;

    const scopeLabel = r.scopeKind === "feature" ? "feature" : "agent";
    findings.push({
      category: "paid_for_nothing",
      scopeKind: r.scopeKind,
      scopeValue: r.scopeValue,
      // Directly observed wasted spend -> always a bounded recoverable, never null.
      recoverableMicroUsd: wasted,
      windowSpendMicroUsd: scopeSpend,
      confidence: "high",
      title: `Billed runs that returned nothing on ${scopeLabel} "${r.scopeValue}"`,
      reason: `${wastedRuns} billed run(s) on this ${scopeLabel} ended failed or abandoned, so ${shareOfScopeSpend}% of its spend bought no result.`,
      evidence: {
        failedRuns,
        abandonedRuns,
        exampleTrace: r.exampleTrace,
        shareOfScopeSpend,
      },
      // The run view (/agents) is where a user inspects failed/abandoned runs.
      drillHref: "/agents",
    });
  }
  return findings;
}

/**
 * Build the feature / agent multi-select clauses for this detector. `filters.feature` narrows
 * `FeatureTag`, `filters.agent` narrows `ServiceName`; every other dimension is irrelevant here and
 * ignored. Values are bound as `Array(String)` params (never interpolated), exactly like the other
 * ClickHouse reads in this codebase.
 *
 * `filters` is typed as {@link DimensionFilters} (feature/model/layer/provider/account). "agent" is
 * not one of those dimension keys, so we read it defensively off the record -- the aggregation
 * endpoint (W7) may pass an agent narrowing under that key without it being a formal Dimension.
 */
function detectorFilters(filters: DimensionFilters): {
  clause: string;
  params: Record<string, string[]>;
} {
  const clauses: string[] = [];
  const params: Record<string, string[]> = {};

  const features = filters.feature ?? [];
  if (features.length > 0) {
    clauses.push("AND FeatureTag IN {f_features:Array(String)}");
    params.f_features = features;
  }
  const agents = (filters as Record<string, string[] | undefined>).agent ?? [];
  if (agents.length > 0) {
    clauses.push("AND ServiceName IN {f_agents:Array(String)}");
    params.f_agents = agents;
  }

  return { clause: clauses.join(" "), params };
}

/**
 * Entry point (W7 calls this): run the paid-for-nothing query for the window + filters, map through
 * the pure detector, and return the findings. Returns `[]` when ClickHouse is unreachable (`tryLive`
 * returns null) -- no mock/static fallback, so a blank stack is honest rather than fabricated.
 *
 * The query works in two stages, both anchored on the ClickHouse clock (`now() - INTERVAL {w} DAY`),
 * never the Node clock (CTO-203):
 *   1. Collapse spans into runs (per TraceId): total run cost and whether the run ended in error
 *      (`max(StatusCode) = 2`), matching `queryAgents` exactly. A run also carries its feature tag
 *      and its agent (ServiceName). `GenAiOperation NOT IN ('compute','egress')` keeps this to
 *      run-shaped, billable spend (the synthetic infra rows are not runs).
 *   2. Assign each run to EXACTLY ONE scope (its feature when tagged, else its agent) and roll those
 *      single-scope groups up, summing wasted cost (cost of the runs that ended failed) and counting
 *      the wasted runs. One run's dollars land in one scope, never two (CTO-227 review finding, Bug 2:
 *      the previous by-feature-AND-by-agent union double-counted the recoverable total).
 *   3. Separately, compute each scope's TRUE total spend (all runs of that FeatureTag; all runs of
 *      that ServiceName, feature-tagged ones included) and join it in as the share DENOMINATOR
 *      (CTO-227 review pass 2). The wasted attribution stays single-scoped so the recoverable total
 *      still counts each dollar once, but the denominator is no longer the single-scoped subtotal;
 *      that subtotal made an agent's `shareOfScopeSpend` read 100% when its feature-tagged spend was
 *      excluded.
 */
export async function collectPaidForNothing(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  const found = await tryLive(async (db, tenant) => {
    const w = clampWindowDays(windowDays);
    const { clause, params } = detectorFilters(filters);

    // Stage 1 (runs subquery): one row per TraceId with its cost, terminal-error flag, feature and
    // agent. `isFailed` reuses queryAgents' derivation: max(StatusCode)=2 (OTel Error) => failed.
    // Stage 2 (scoped/wasted): attribute each run to ONE scope (feature when tagged, else agent), then
    // GROUP BY that scope for the wasted spend and the wasted-run counts. One run's dollars count once
    // (CTO-227 Bug 2). `wastedCost`/`failedRuns` gate on the run being both billed (runCost > 0) AND
    // failed. `abandonedRuns` is 0: abandonment is not derivable from OTel StatusCode today (see
    // queryAgents), but the column is kept so the shape and the evidence are stable if a future signal
    // lands.
    // Stage 3 (scope_totals): the TRUE per-scope total spend, computed independently of the
    // single-scope assignment: every FeatureTag run for a feature scope, every ServiceName run
    // (feature-tagged included) for an agent scope. Joined in as `scopeCost` so `shareOfScopeSpend`
    // divides by the real total, not the single-scoped subtotal (CTO-227 review pass 2).
    const runsCte = `
      SELECT
        TraceId AS runId,
        any(FeatureTag) AS feature,
        any(ServiceName) AS agent,
        sum(EstimatedCost) AS runCost,
        max(StatusCode) = 2 AS isFailed
      FROM otel_spans
      WHERE TenantId = {tenant:String}
        AND Timestamp >= now() - INTERVAL ${w} DAY
        AND GenAiOperation NOT IN ('compute', 'egress')
        ${clause}
      GROUP BY TraceId`;

    // A wasted run: billed (runCost > 0) and failed. We express the gate once as a reusable flag.
    const wastedRunExpr = "(runCost > 0 AND isFailed)";

    // CTO-227 review finding (Bug 2): assign each run to EXACTLY ONE scope before rolling up, so its
    // dollars are counted once. The old query rolled the same runs up BY feature AND BY agent in a
    // UNION ALL, so a failed feature-tagged run on a named agent surfaced as TWO findings whose
    // recoverables BOTH fed `aggregateWaste`'s total — overstating "Recoverable" up to 2x. Here a run
    // with a FeatureTag is attributed to its feature; an untagged run falls back to its agent. Runs
    // that are neither tagged nor on a usable agent cannot be attributed to any scope, so they are
    // dropped rather than double-counted or mis-assigned.
    const scopeKindExpr = "if(feature != '', 'feature', 'agent')";
    const scopeValueExpr = "if(feature != '', feature, agent)";

    // TRUE per-scope totals (the share denominator). Kept apart from the single-scope assignment so an
    // agent's total includes its feature-tagged runs. Empty/`unknown` scope keys are dropped to match
    // the `scoped` filter, so every wasted scope has exactly one totals row to join.
    const scopeTotalsCte = `
      SELECT 'feature' AS scopeKind, feature AS scopeValue, sum(runCost) AS totalCost
      FROM runs
      WHERE feature != ''
      GROUP BY feature
      UNION ALL
      SELECT 'agent' AS scopeKind, agent AS scopeValue, sum(runCost) AS totalCost
      FROM runs
      WHERE agent != '' AND agent != 'unknown'
      GROUP BY agent`;

    const sql = `
      WITH runs AS (${runsCte}),
      scoped AS (
        SELECT
          ${scopeKindExpr} AS scopeKind,
          ${scopeValueExpr} AS scopeValue,
          runCost,
          isFailed,
          runId
        FROM runs
        WHERE NOT (feature = '' AND (agent = '' OR agent = 'unknown'))
      ),
      wasted AS (
        SELECT
          scopeKind,
          scopeValue,
          sumIf(runCost, ${wastedRunExpr}) AS wastedCostNum,
          countIf(${wastedRunExpr}) AS failedRunsNum,
          anyIf(runId, ${wastedRunExpr}) AS exampleTraceVal
        FROM scoped
        GROUP BY scopeKind, scopeValue
        HAVING sumIf(runCost, ${wastedRunExpr}) > 0
      ),
      scope_totals AS (${scopeTotalsCte})
      SELECT
        w.scopeKind AS scopeKind,
        w.scopeValue AS scopeValue,
        toString(w.wastedCostNum) AS wastedCost,
        toString(t.totalCost) AS scopeCost,
        toString(w.failedRunsNum) AS failedRuns,
        '0' AS abandonedRuns,
        w.exampleTraceVal AS exampleTrace
      FROM wasted AS w
      LEFT JOIN scope_totals AS t
        ON w.scopeKind = t.scopeKind AND w.scopeValue = t.scopeValue`;

    const raw = await rowsP<PaidForNothingRow>(db, sql, { tenant, ...params });
    return detectPaidForNothing(raw);
  });

  // tryLive returns null on unreachable ClickHouse; the contract is [] in that case.
  return found ?? [];
}
