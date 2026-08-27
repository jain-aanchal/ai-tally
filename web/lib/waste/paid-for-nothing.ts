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
  /** Summed EstimatedCost of ALL runs in this scope over the window. Decimal-USD string. */
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
 * total spend over the window, so the finding can show wasted spend as a share of the whole.
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
 *   2. Roll runs up BY feature and BY agent separately, summing wasted cost (cost of the runs that
 *      ended failed) and total scope cost, and counting the wasted runs.
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
    // Stage 2 (outer): two GROUP BYs unioned -- by feature and by agent -- each emitting a scope row
    // with wasted vs. total spend and the wasted-run counts. `wastedCost`/`failedRuns` gate on the
    // run being both billed (runCost > 0) AND failed. `abandonedRuns` is 0: abandonment is not
    // derivable from OTel StatusCode today (see queryAgents), but the column is kept so the shape and
    // the evidence are stable if a future signal lands.
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

    const sql = `
      WITH runs AS (${runsCte})
      SELECT
        'feature' AS scopeKind,
        feature AS scopeValue,
        toString(sumIf(runCost, ${wastedRunExpr})) AS wastedCost,
        toString(sum(runCost)) AS scopeCost,
        toString(countIf(${wastedRunExpr})) AS failedRuns,
        '0' AS abandonedRuns,
        anyIf(runId, ${wastedRunExpr}) AS exampleTrace
      FROM runs
      WHERE feature != ''
      GROUP BY feature
      HAVING sumIf(runCost, ${wastedRunExpr}) > 0

      UNION ALL

      SELECT
        'agent' AS scopeKind,
        agent AS scopeValue,
        toString(sumIf(runCost, ${wastedRunExpr})) AS wastedCost,
        toString(sum(runCost)) AS scopeCost,
        toString(countIf(${wastedRunExpr})) AS failedRuns,
        '0' AS abandonedRuns,
        anyIf(runId, ${wastedRunExpr}) AS exampleTrace
      FROM runs
      WHERE agent != '' AND agent != 'unknown'
      GROUP BY agent
      HAVING sumIf(runCost, ${wastedRunExpr}) > 0`;

    const raw = await rowsP<PaidForNothingRow>(db, sql, { tenant, ...params });
    return detectPaidForNothing(raw);
  });

  // tryLive returns null on unreachable ClickHouse; the contract is [] in that case.
  return found ?? [];
}
