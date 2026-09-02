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
import { tryLive, rowsPCached, micro } from "@/lib/clickhouse";
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
 * The query is a SINGLE pass over the base data, anchored on the ClickHouse clock
 * (`now() - INTERVAL {w} DAY`), never the Node clock (CTO-203):
 *   1. Collapse spans into runs (per TraceId): total run cost and whether the run ended in error
 *      (`max(StatusCode) = 2`), matching `queryAgents` exactly. A run also carries its feature tag
 *      and its agent (ServiceName). `GenAiOperation NOT IN ('compute','egress')` keeps this to
 *      run-shaped, billable spend (the synthetic infra rows are not runs).
 *   2. Fan each run out (ARRAY JOIN) into the scope rows it belongs to: its feature scope (when
 *      tagged) and its agent scope (when on a usable ServiceName). Every fanned row carries the run's
 *      FULL cost as the scope TOTAL, but the WASTED cost only in the run's single "waste scope" (its
 *      feature when tagged, else its agent). Grouping those rows by scope therefore yields, in one
 *      aggregate, both the scope's TRUE total spend (an agent's includes its feature-tagged runs) and
 *      its wasted spend attributed exactly once (CTO-227 review finding Bug 2: no by-feature-AND-by-
 *      agent double count; review pass 2: the denominator is the true total, not the single-scoped
 *      subtotal, so an agent's `shareOfScopeSpend` is not inflated to 100%).
 *
 * CTO-227 review pass 3 (Fix 1+2): this replaces a `runs` CTE that was referenced THREE times (once
 * for the wasted roll-up, twice in a `scope_totals` UNION). ClickHouse inlines a multiply-referenced
 * WITH, so the otel_spans scan + group ran ~3x per call on a user-facing hot path; the single grouped
 * pass here scans the base data ONCE. It also drops the LEFT JOIN to `scope_totals`, whose 0-fill
 * (`join_use_nulls=0`) could have emitted `windowSpendMicroUsd = 0` alongside `recoverable > 0` had the
 * two WHERE clauses ever drifted, a fabricated 0 that breaks the honest-blank invariant. By
 * construction each scope's wasted cost is a subset of the SAME aggregate's total cost, so a scope can
 * never emit `windowSpendMicroUsd < recoverableMicroUsd`.
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
    // `GenAiOperation NOT IN ('compute','egress')` keeps this to run-shaped, billable spend.
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

    // A wasted run: billed (runCost > 0) and failed (terminal error). Expressed once as a reusable gate.
    const wastedRunExpr = "(runCost > 0 AND isFailed)";

    // Stage 2 (single grouped pass): fan each run out into the scope rows it belongs to, then GROUP BY
    // scope ONCE. This replaces the old scoped/wasted + scope_totals-UNION-and-LEFT-JOIN shape, which
    // referenced the `runs` CTE three times (ClickHouse inlines a multiply-referenced WITH, so the
    // otel_spans scan ran ~3x) and 0-filled the total on any clause drift (CTO-227 review pass 3,
    // Fix 1+2).
    //
    // Each run emits up to two membership tuples via ARRAY JOIN:
    //   (present, scopeKind, scopeValue, totalCost, wastedCost, wastedCount, exampleTrace)
    // - feature membership (present when FeatureTag is set): totalCost = the run's full cost; the run's
    //   whole wasted cost lands HERE, because a tagged run's single "waste scope" is its feature.
    // - agent membership (present on a usable ServiceName): totalCost = the run's full cost too, so an
    //   agent's total includes its feature-tagged runs (CTO-227 review pass 2 denominator); but its
    //   wasted cost is non-zero ONLY for an UNTAGGED run, whose single waste scope is the agent. A
    //   tagged run therefore contributes its wasted dollars to exactly one scope (CTO-227 Bug 2), never
    //   two. A run that is neither tagged nor on a usable agent emits no membership and is dropped.
    // Because both memberships carry the same full totalCost while the wasted cost is a subset of it,
    // sum(wastedCost) <= sum(totalCost) within every group by construction: no scope can ever report
    // windowSpend < recoverable, and there is no join to 0-fill.
    //
    // Money stays in ClickHouse Decimal end to end: `EstimatedCost` is Decimal(38,8), so the wasted
    // cost is `runCost * flag` (Decimal x UInt8 -> Decimal), never `if(cond, runCost, 0.0)`, whose
    // Float64 zero would both break type unification AND float-ify money (CLAUDE.md: never float
    // dollars). `toString` hands the pure detector a decimal string, exactly as before.
    const featurePresent = "toUInt8(feature != '')";
    const agentPresent = "toUInt8(agent != '' AND agent != 'unknown')";
    // Wasted attributed to the agent scope only when the run is UNTAGGED (else it is the feature's).
    const agentWasted = `(${wastedRunExpr} AND feature = '')`;

    const sql = `
      WITH runs AS (${runsCte})
      SELECT
        m.2 AS scopeKind,
        m.3 AS scopeValue,
        toString(sum(m.5)) AS wastedCost,
        toString(sum(m.4)) AS scopeCost,
        toString(sum(m.6)) AS failedRuns,
        '0' AS abandonedRuns,
        anyIf(m.7, m.7 != '') AS exampleTrace
      FROM runs
      ARRAY JOIN arrayFilter(t -> t.1 = 1, [
        (
          ${featurePresent}, 'feature', feature, runCost,
          runCost * toUInt8(${wastedRunExpr}), toUInt8(${wastedRunExpr}),
          if(${wastedRunExpr}, runId, '')
        ),
        (
          ${agentPresent}, 'agent', agent, runCost,
          runCost * toUInt8(${agentWasted}), toUInt8(${agentWasted}),
          if(${agentWasted}, runId, '')
        )
      ]) AS m
      GROUP BY scopeKind, scopeValue
      HAVING sum(m.5) > 0`;

    const raw = await rowsPCached<PaidForNothingRow>(db, sql, { tenant, ...params });
    return detectPaidForNothing(raw);
  });

  // tryLive returns null on unreachable ClickHouse; the contract is [] in that case.
  return found ?? [];
}
