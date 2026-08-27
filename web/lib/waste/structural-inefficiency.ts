// SPDX-License-Identifier: Apache-2.0
// The "structural inefficiency" waste detector (CTO-233, W6; epic CTO-227). This is one detector in
// the waste family: it looks at the SHAPE of a run rather than its outcome, and flags runs whose
// shape deviates sharply from the norm for their OWN feature or agent.
//
// Two body-free shape signals, and both are measured against a run's own cohort, never a global
// average, because "big" is only meaningful relative to what that feature/agent normally does:
//
//   1. Prompt/context bloat  -- a run's input-token total sits far above the median for the SAME
//      feature+model. The recoverable figure is the marginal cost of the tokens above that median:
//      what the run would have cost had it carried a typical prompt/context for its cohort.
//   2. Runaway agent loops   -- a run's step (span) count sits far above the median for its agent.
//      The recoverable figure is the marginal cost of the steps above the median-length run.
//
// HONESTY (CTO-227). This detector flags CANDIDATES to inspect, not certainties. A legitimately
// long-context feature (a summarizer that always ingests 100k tokens) is NOT waste: the signal is
// DEVIATION from the feature's own norm, so a uniformly heavy feature produces no outliers and no
// finding. Every finding states the median it compared against in `evidence`, so a reader who knows
// a scope is heavy on purpose can dismiss it on sight. Confidence is 'medium', never 'high'.
//
// ROBUST STATS, not mean+stddev (CTO-233). A single runaway run drags a mean up and inflates a
// standard deviation, so a mean+stddev fence both moves toward the outlier it is meant to catch AND
// flags half a uniformly heavy cohort. We use the median and a Tukey IQR fence (both resistant to a
// few extreme values), plus a relative floor (>= a multiple of the median) that guards the case where
// a tight, duplicate-heavy cohort collapses the IQR to zero and would otherwise flag a hair above
// the median. A value must clear BOTH gates to be an outlier.
//
// NO BODIES (CLAUDE.md). Only token counts, span counts, cost and trace ids are read. No prompt,
// completion or retrieved text is touched; `exampleTrace` is a trace id, a pointer, not content.

import type { MicroUSD } from "@/lib/types";
import type { WasteFinding } from "@/lib/waste";
import type { DimensionFilters } from "@/lib/filters";
import { clampWindowDays } from "@/lib/explore";
import { tryLive, rowsP, micro } from "@/lib/clickhouse";

/**
 * One run reduced to the shape signals this detector judges. The SQL below produces exactly these
 * rows (one per trace); the pure detector never sees a span, a prompt or a completion.
 */
export interface StructuralRunRow {
  runId: string;
  /** Agent identity = ServiceName (matches queryAgents), the cohort for the runaway-loop signal. */
  agent: string;
  /** Feature tag (or 'untagged'), the scope the context-bloat finding is attributed to. */
  feature: string;
  /** Model that carried the most input tokens on the run; the bloat cohort is feature+model. */
  model: string;
  inputTokens: number;
  outputTokens: number;
  /** Span count for the run, exactly the `steps` queryAgents computes. */
  steps: number;
  costMicroUsd: MicroUSD;
}

// --- Thresholds (all named, all with a WHY; CTO-233) ---------------------------------------------

// A cohort needs enough peers for its median to mean anything. Below this we do not judge a run
// against the cohort at all (honest-under-uncertainty: a "median" of two runs is not a norm). Five
// is the smallest count at which an IQR has interior points on both sides of the median.
const MIN_COHORT_RUNS = 5;

// Tukey fence multiplier. The classic "mild outlier" fence is 1.5*IQR; 3.0 is the standard "far out"
// fence. We want clear structural deviation, not the merely-above-average, so we use the far-out
// fence: an outlier sits past Q3 + 3*IQR.
const IQR_FENCE = 3;

// Relative floor. A run must ALSO be at least this multiple of the cohort median to flag. This is the
// guard the IQR fence needs when a cohort is tight and duplicate-heavy (IQR collapses to 0 and the
// fence degenerates to "> Q3"): without it, a uniformly heavy feature would flag every run a token
// above its median, the exact false positive the honesty posture forbids. 3x means "several times the
// norm", which no member of a genuinely uniform cohort can reach.
const MEDIAN_FLOOR_MULT = 3;

// Below this many input tokens a run is too small to call "context bloat" whatever its ratio to the
// median: 3x of a 300-token median is pocket change and would only add noise to the report.
const MIN_BLOAT_INPUT_TOKENS = 4_000;

// Below this many steps a run cannot be a "runaway loop" whatever its ratio to the median: a 3x of a
// 2-step median is a 6-step run, which is a normal agent, not a loop that ran away.
const MIN_RUNAWAY_STEPS = 6;

// --- Robust statistics (pure, local; the ones in clickhouse.ts are module-private) ---------------

function median(xs: number[]): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** Linear-interpolated quantile (q in [0,1]). Interpolation keeps Q1/Q3 stable on small cohorts. */
function quantile(xs: number[], q: number): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  if (s.length === 1) return s[0];
  const pos = q * (s.length - 1);
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  if (lo === hi) return s[lo];
  return s[lo] + (s[hi] - s[lo]) * (pos - lo);
}

/**
 * The robust upper gate for a cohort of `values`: a value is an outlier when it clears BOTH the Tukey
 * far-out fence (Q3 + IQR_FENCE*IQR) AND the relative floor (MEDIAN_FLOOR_MULT * median). Returns the
 * median (reported in evidence) and the effective cutoff a value must strictly exceed.
 */
function outlierGate(values: number[]): { median: number; cutoff: number } {
  const med = median(values);
  const q1 = quantile(values, 0.25);
  const q3 = quantile(values, 0.75);
  const iqr = q3 - q1;
  const tukey = q3 + IQR_FENCE * iqr;
  const floor = med * MEDIAN_FLOOR_MULT;
  // Both gates must be cleared, so the binding cutoff is the LARGER of the two.
  return { median: med, cutoff: Math.max(tukey, floor) };
}

// --- Marginal recoverable cost -------------------------------------------------------------------
//
// The recoverable figure is what the run would NOT have cost had it behaved like the cohort median.
// We do not have a per-token or per-step price on the row, so we attribute the run's own cost in
// proportion to the excess: the fraction of the run's input tokens (or steps) that sits above the
// median maps to that same fraction of the run's cost. This is a deliberate LOWER-effort bound, not a
// repricing: it never claims to recover more than the run actually cost, and it is 0 (rendered as an
// honest blank upstream, never a fabricated number) when the excess maps to no cost.

function marginalCost(costMicroUsd: number, excess: number, whole: number): MicroUSD {
  if (whole <= 0 || excess <= 0) return 0;
  return Math.round((costMicroUsd * excess) / whole);
}

// --- Pure detector -------------------------------------------------------------------------------

/** Group rows by a key into an ordered Map (insertion order, so output is deterministic). */
function groupBy<T>(rows: T[], key: (r: T) => string): Map<string, T[]> {
  const m = new Map<string, T[]>();
  for (const r of rows) {
    const k = key(r);
    const list = m.get(k);
    if (list) list.push(r);
    else m.set(k, [r]);
  }
  return m;
}

/** Total cost of a set of runs, in micro-USD. */
function totalCost(rows: StructuralRunRow[]): MicroUSD {
  return rows.reduce((s, r) => s + r.costMicroUsd, 0);
}

interface Outlier {
  run: StructuralRunRow;
  cohortMedian: number;
  recoverable: MicroUSD;
}

/** Round a median for prose/evidence (it can be a .5 from an even-length cohort). */
function med0(n: number): number {
  return Math.round(n);
}

/**
 * Prompt/context-bloat findings. The median is per feature+model (a feature is judged against itself,
 * split by model because different models carry very different typical context sizes). Outlier runs
 * are then rolled up to ONE finding per FEATURE (the scope), so the scope+title key stays unique and
 * findings do not collapse in the roll-up's de-dupe.
 */
function detectContextBloat(rows: StructuralRunRow[]): WasteFinding[] {
  const outliersByFeature = new Map<string, Outlier[]>();

  for (const cohort of groupBy(rows, (r) => `${r.feature} ${r.model}`).values()) {
    if (cohort.length < MIN_COHORT_RUNS) continue;
    const { median: med, cutoff } = outlierGate(cohort.map((r) => r.inputTokens));
    for (const run of cohort) {
      if (run.inputTokens <= cutoff) continue;
      if (run.inputTokens < MIN_BLOAT_INPUT_TOKENS) continue;
      const excess = run.inputTokens - med;
      const whole = run.inputTokens + run.outputTokens;
      const outlier: Outlier = {
        run,
        cohortMedian: med,
        recoverable: marginalCost(run.costMicroUsd, excess, whole),
      };
      const list = outliersByFeature.get(run.feature);
      if (list) list.push(outlier);
      else outliersByFeature.set(run.feature, [outlier]);
    }
  }

  const featureTotals = groupBy(rows, (r) => r.feature);
  const findings: WasteFinding[] = [];
  for (const [feature, outliers] of outliersByFeature) {
    // The single worst run represents the finding (largest input-token total); its cohort median is
    // the number a reader compares against to decide the feature is legitimately heavy.
    const worst = outliers.reduce((a, b) => (b.run.inputTokens > a.run.inputTokens ? b : a));
    const recoverable = outliers.reduce((s, o) => s + o.recoverable, 0);
    const ratio =
      worst.run.outputTokens > 0
        ? Math.round((worst.run.inputTokens / worst.run.outputTokens) * 10) / 10
        : worst.run.inputTokens;
    findings.push({
      category: "structural_inefficiency",
      scopeKind: "feature",
      scopeValue: feature,
      // 0 recoverable is an honest blank, never a fabricated number (CTO-227).
      recoverableMicroUsd: recoverable > 0 ? recoverable : null,
      windowSpendMicroUsd: totalCost(featureTotals.get(feature) ?? []),
      confidence: "medium",
      title: `Context bloat on ${feature}`,
      reason:
        `${outliers.length} run(s) on ${feature} carried input-token totals far above the ` +
        `feature+model median (${med0(worst.cohortMedian)} vs ${worst.run.inputTokens} tokens). ` +
        `Recoverable is the marginal cost of the tokens above the median.`,
      evidence: {
        signal: "context-bloat",
        median: med0(worst.cohortMedian),
        observed: worst.run.inputTokens,
        exampleTrace: worst.run.runId,
        model: worst.run.model,
        inputOutputRatio: ratio,
        outlierRuns: outliers.length,
      },
      drillHref: "/agents",
    });
  }
  return findings;
}

/**
 * Runaway-agent-loop findings. The median is per AGENT (ServiceName), which is also the scope, so one
 * finding per agent with any outlier run. The recoverable figure is the marginal cost of the steps
 * above the agent's median-length run.
 */
function detectRunawayLoops(rows: StructuralRunRow[]): WasteFinding[] {
  const findings: WasteFinding[] = [];

  for (const [agent, cohort] of groupBy(rows, (r) => r.agent)) {
    if (cohort.length < MIN_COHORT_RUNS) continue;
    const { median: med, cutoff } = outlierGate(cohort.map((r) => r.steps));
    const outliers: Outlier[] = [];
    for (const run of cohort) {
      if (run.steps <= cutoff) continue;
      if (run.steps < MIN_RUNAWAY_STEPS) continue;
      const excess = run.steps - med;
      outliers.push({
        run,
        cohortMedian: med,
        recoverable: marginalCost(run.costMicroUsd, excess, run.steps),
      });
    }
    if (outliers.length === 0) continue;

    const worst = outliers.reduce((a, b) => (b.run.steps > a.run.steps ? b : a));
    const recoverable = outliers.reduce((s, o) => s + o.recoverable, 0);
    findings.push({
      category: "structural_inefficiency",
      scopeKind: "agent",
      scopeValue: agent,
      recoverableMicroUsd: recoverable > 0 ? recoverable : null,
      windowSpendMicroUsd: totalCost(cohort),
      confidence: "medium",
      title: `Runaway loops on ${agent}`,
      reason:
        `${outliers.length} run(s) on ${agent} took far more steps than the agent's median-length ` +
        `run (${med0(worst.cohortMedian)} vs ${worst.run.steps} steps). Recoverable is the marginal ` +
        `cost of the steps above the median.`,
      evidence: {
        signal: "runaway-loop",
        median: med0(worst.cohortMedian),
        observed: worst.run.steps,
        exampleTrace: worst.run.runId,
        outlierRuns: outliers.length,
      },
      drillHref: "/agents",
    });
  }
  return findings;
}

/**
 * PURE. Given one row per run, emit structural-inefficiency findings (context bloat + runaway loops).
 * No queries, no clock, no I/O: deterministic in its input, which is what makes it unit-testable.
 */
export function detectStructuralInefficiency(rows: StructuralRunRow[]): WasteFinding[] {
  return [...detectContextBloat(rows), ...detectRunawayLoops(rows)];
}

// --- Live collection -----------------------------------------------------------------------------

// Response model when the provider returned one (it is the model that actually served the call),
// request model otherwise, 'unknown' when neither is set. Matches EXPLORE_GROUP_EXPR / scopeFilter so
// the model dimension means the same thing everywhere.
const MODEL_EXPR =
  "if(GenAiResponseModel != '', GenAiResponseModel, if(GenAiRequestModel != '', GenAiRequestModel, 'unknown'))";

/**
 * Build the dimension-filter clauses this detector honors, bound as Array(String) params (never
 * string-concatenated, so no user text reaches the SQL text). DimensionFilters carries no `agent`
 * dimension (agents are ServiceName, which is not a filter dimension in filters.ts), so the requested
 * agent filter has no field to bind to; feature and model still scope the scan, which narrows the
 * runaway-loop cohorts indirectly (only runs touching the kept features/models are read).
 */
function filterClauses(filters: DimensionFilters): {
  clause: string;
  params: Record<string, string[]>;
} {
  const clauses: string[] = [];
  const params: Record<string, string[]> = {};
  if (filters.feature.length > 0) {
    clauses.push("AND FeatureTag IN {f_feature:Array(String)}");
    params.f_feature = filters.feature;
  }
  if (filters.model.length > 0) {
    clauses.push(`AND ${MODEL_EXPR} IN {f_model:Array(String)}`);
    params.f_model = filters.model;
  }
  return { clause: clauses.join(" "), params };
}

interface StructuralRunRaw {
  runId: string;
  agent: string;
  feature: string;
  model: string;
  inputTokens: string;
  outputTokens: string;
  steps: string;
  cost: string;
}

/**
 * Collect structural-inefficiency findings for the window/filters against live ClickHouse.
 *
 * The window is clamped (clampWindowDays) and rolling off ClickHouse's own clock (never the Node
 * clock; CTO-203), matching queryAgents. Returns `[]` when ClickHouse is unreachable (via tryLive) or
 * when there are simply no outliers: there is NO static fallback, so an empty result means "nothing
 * to flag", never fabricated waste.
 */
export async function collectStructuralInefficiency(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  const found = await tryLive(async (db, tenant) => {
    const w = clampWindowDays(windowDays);
    const { clause, params } = filterClauses(filters);
    // One row per run (trace). feature/model are the argMax over the run's tokens so a mixed-model run
    // is judged in the cohort of the model that actually did the work. steps = span count, exactly
    // what queryAgents reports. Compute/egress spans are excluded: they are tenant infrastructure, not
    // agent steps, and would inflate both signals. `w` is a clamped int (injection-safe).
    const raw = await rowsP<StructuralRunRaw>(
      db,
      `SELECT TraceId AS runId,
              any(ServiceName) AS agent,
              argMax(if(FeatureTag != '', FeatureTag, 'untagged'), InputTokens + OutputTokens) AS feature,
              argMax(${MODEL_EXPR}, InputTokens) AS model,
              sum(InputTokens) AS inputTokens,
              sum(OutputTokens) AS outputTokens,
              count() AS steps,
              sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL ${w} DAY
         AND ServiceName != '' AND ServiceName != 'unknown'
         AND GenAiOperation NOT IN ('compute', 'egress')
         ${clause}
       GROUP BY TraceId`,
      { tenant, ...params },
    );
    const runs: StructuralRunRow[] = raw.map((r) => ({
      runId: r.runId,
      agent: r.agent || "untagged",
      feature: r.feature || "untagged",
      model: r.model || "unknown",
      inputTokens: parseInt(r.inputTokens, 10) || 0,
      outputTokens: parseInt(r.outputTokens, 10) || 0,
      steps: parseInt(r.steps, 10) || 0,
      costMicroUsd: micro(r.cost),
    }));
    return detectStructuralInefficiency(runs);
  });
  return found ?? [];
}
