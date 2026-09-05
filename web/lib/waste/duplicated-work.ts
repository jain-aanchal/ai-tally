// SPDX-License-Identifier: Apache-2.0
// The "duplicated and retried work" waste detector (CTO-230, W3; epic CTO-227).
//
// What it finds: money a tenant paid to do a unit of work that a later retry made redundant. ONE
// shape, and only one, carries a recoverable dollar figure:
//   error-then-retry: a run that errored, then another run of the same shape ran moments later
//     and (in at least one later attempt) succeeded. The failed attempt is spend that produced
//     nothing once the retry landed. We can stand behind that: a run actually FAILED, so its cost
//     bought nothing, and the recoverable dollars are exactly the failed attempts' cost.
//
// What it deliberately does NOT quantify (CTO-227; review: rapid-repeat conflated with normal
// conversation): a plain burst of same-shape runs with NO failure among them. Telemetry carries no
// prompt or completion text (see CLAUDE.md, "No bodies in telemetry"), so a rapid repeat is
// indistinguishable from a legitimate multi-turn session: a user asking follow-up questions of a
// chatbot looks exactly like a user re-running the same request. On the demo that "rapid-repeat"
// rule fired on ordinary conversation and fabricated ~$19.7k of "recoverable" spend that was not
// real, dominating the page's Recoverable headline with a number we could not defend. Honest under
// uncertainty means we do not put a dollar on it: a pure repeat with no error produces NO finding.
//
// HEURISTIC even for what we do keep, and honest about it: "same shape" is same feature + agent +
// model + user landing within a short window, not proof of identical inputs. So the failure signal
// is what earns the dollars, and every finding is `confidence: 'medium'` with a `reason` that says
// so out loud. A run's identity here is exactly those four dimensions; the model expression mirrors
// queryCostExplore's `groupBy: 'model'` case so "same model" means the same thing on this surface as
// everywhere else on the dashboard.

import type { WasteFinding } from "../waste";
import type { DimensionFilters } from "../filters";
import { clampWindowDays } from "../explore";
import { tryLive, rowsPCached, micro } from "../clickhouse";

// --- Named thresholds (WHY, not magic numbers) --------------------------------------------------

// CTO-230: a retry chases its failed predecessor within seconds-to-minutes, not hours. Five minutes
// is generous enough to catch a human-or-framework retry loop (backoff, a "try again" click) while
// staying far under the gap between two genuinely independent pieces of work for the same user. Two
// same-shape runs separated by more than this are treated as unrelated, not as a duplicate.
const RETRY_WINDOW_SECONDS = 5 * 60;

// --- Pure detector input --------------------------------------------------------------------------

/** OTel StatusCode == 2 is Error (see toRunSpan / queryLiveMetrics). Everything else reads success. */
export type RunOutcome = "success" | "failed";

/** One run in a cluster: a whole trace, reduced to the fields the heuristic needs. */
export interface DuplicatedWorkRun {
  traceId: string;
  /** Unix seconds (ClickHouse `toUnixTimestamp(max(Timestamp))`), so proximity is clock-consistent. */
  timestampSec: number;
  costMicroUsd: number;
  outcome: RunOutcome;
}

/**
 * Runs that share the four identity dimensions: feature + agent + model + user. `feature` drives the
 * finding scope (feature when tagged, else the agent). Runs need not be pre-sorted; the detector
 * orders them by time itself.
 */
export interface DuplicatedWorkCluster {
  feature: string;
  agent: string;
  model: string;
  userIdHash: string;
  runs: DuplicatedWorkRun[];
}

// --- Pure detection -------------------------------------------------------------------------------

/** Maximal runs of runs where each is within RETRY_WINDOW_SECONDS of its predecessor (a "burst"). */
function burstsOf(runs: DuplicatedWorkRun[]): DuplicatedWorkRun[][] {
  const sorted = [...runs].sort((a, b) => a.timestampSec - b.timestampSec);
  const bursts: DuplicatedWorkRun[][] = [];
  let current: DuplicatedWorkRun[] = [];
  for (const r of sorted) {
    if (current.length === 0) {
      current = [r];
      continue;
    }
    const prev = current[current.length - 1];
    if (r.timestampSec - prev.timestampSec <= RETRY_WINDOW_SECONDS) current.push(r);
    else {
      bursts.push(current);
      current = [r];
    }
  }
  if (current.length > 0) bursts.push(current);
  return bursts;
}

/** One finding under construction, accumulated across clusters that share scope + agent + model. */
interface Accumulator {
  scopeKind: "feature" | "agent";
  scopeValue: string;
  agent: string;
  model: string;
  recoverableMicroUsd: number;
  supersededRuns: number;
  /** Observed spend on the runs that make up this finding (superseded plus the kept survivor). */
  windowSpendMicroUsd: number;
  /** Widest burst span contributing to this finding, in seconds. */
  windowSeconds: number;
  exampleTrace: string;
}

/** Accumulation key: same scope + agent + model collapse into one finding. */
function accKey(a: {
  scopeKind: string;
  scopeValue: string;
  agent: string;
  model: string;
}): string {
  return JSON.stringify([a.scopeKind, a.scopeValue, a.agent, a.model]);
}

/**
 * Detect duplicated/retried work over pre-grouped clusters. PURE and deterministic: no queries, no
 * clock, no randomness, so it is trivially testable.
 *
 * Per cluster, runs are split into time bursts. Within each burst we look for the ONE thing we can
 * defend: a failed run superseded by a LATER success in the same burst. Those failed attempts are
 * the recoverable, superseded spend; their cost is what stopping the waste would save.
 *
 * A burst with no such error-then-success sequence yields nothing (CTO-227; review: rapid-repeat
 * conflated with normal conversation). Without message bodies a plain burst of same-shape runs is
 * indistinguishable from a legitimate multi-turn session, so we refuse to claim dollars for it: no
 * failure, no finding.
 *
 * Findings sharing scope + agent + model are merged (summed) so multiple users or multiple bursts of
 * the same slice roll into one honest line rather than many near-duplicates.
 */
export function detectDuplicatedWork(clusters: DuplicatedWorkCluster[]): WasteFinding[] {
  const accs = new Map<string, Accumulator>();

  for (const cluster of clusters) {
    const scopeKind: "feature" | "agent" = cluster.feature ? "feature" : "agent";
    const scopeValue = cluster.feature || cluster.agent || "untagged";

    for (const burst of burstsOf(cluster.runs)) {
      // Only a real failure superseded by a later success is duplicated work we can put a number on.
      const hasSuccess = burst.some((r) => r.outcome === "success");
      const supersededList: DuplicatedWorkRun[] = [];
      for (let i = 0; i < burst.length; i++) {
        if (burst[i].outcome !== "failed") continue;
        // A failed run counts only if some LATER run in the burst succeeded (the retry that
        // replaced it). `hasSuccess` is a cheap prefilter; the inner check enforces order.
        if (hasSuccess && burst.slice(i + 1).some((r) => r.outcome === "success")) {
          supersededList.push(burst[i]);
        }
      }
      if (supersededList.length === 0) continue;

      const burstSpend = burst.reduce((s, r) => s + r.costMicroUsd, 0);
      const windowSeconds = burst[burst.length - 1].timestampSec - burst[0].timestampSec;
      const recoverable = supersededList.reduce((s, r) => s + r.costMicroUsd, 0);

      const key = accKey({ scopeKind, scopeValue, agent: cluster.agent, model: cluster.model });
      const existing = accs.get(key);
      if (existing) {
        existing.recoverableMicroUsd += recoverable;
        existing.supersededRuns += supersededList.length;
        existing.windowSpendMicroUsd += burstSpend;
        existing.windowSeconds = Math.max(existing.windowSeconds, windowSeconds);
      } else {
        accs.set(key, {
          scopeKind,
          scopeValue,
          agent: cluster.agent,
          model: cluster.model,
          recoverableMicroUsd: recoverable,
          supersededRuns: supersededList.length,
          windowSpendMicroUsd: burstSpend,
          windowSeconds,
          exampleTrace: supersededList[0].traceId,
        });
      }
    }
  }

  return [...accs.values()].map(toFinding);
}

/** The heuristic disclaimer, stated out loud so the honesty posture is explicit (CTO-227). */
function heuristicReason(): string {
  const minutes = RETRY_WINDOW_SECONDS / 60;
  // CTO-227 honesty: the recoverable dollars ride on a real FAILURE, not on repetition alone. An
  // errored run followed by a same-shape success is spend the retry made redundant. "Same shape" is
  // same feature, agent, model and user, close in time; no prompt or completion text reaches
  // telemetry, so we cannot compare inputs and the match is approximated, not proven. Hence medium
  // confidence. We do NOT flag pure repeats without a failure: without bodies they are
  // indistinguishable from legitimate multi-turn use, so claiming dollars for them would fabricate
  // waste (review: rapid-repeat conflated with normal conversation).
  return (
    `A failed run was retried within ${minutes} min by a same-shape run that succeeded, so the ` +
    `failed attempt is redundant spend. Matched on run shape and timing, not prompt text, so it is a ` +
    `medium-confidence estimate.`
  );
}

function toFinding(a: Accumulator): WasteFinding {
  const label = `${a.agent || "untagged"} / ${a.model || "unknown"}`;
  return {
    category: "duplicated_work",
    scopeKind: a.scopeKind,
    scopeValue: a.scopeValue,
    recoverableMicroUsd: a.recoverableMicroUsd,
    windowSpendMicroUsd: a.windowSpendMicroUsd,
    confidence: "medium",
    title: `Retried failed work: ${label}`,
    reason: heuristicReason(),
    evidence: {
      pattern: "error-then-retry",
      supersededRuns: a.supersededRuns,
      windowSeconds: a.windowSeconds,
      exampleTrace: a.exampleTrace,
    },
    drillHref: "/agents",
  };
}

// --- Live collection ------------------------------------------------------------------------------

// Same model resolution as queryCostExplore's `groupBy: 'model'` case (EXPLORE_GROUP_EXPR.model):
// the response model when the provider returned one, the request model otherwise, else 'unknown'.
// Inlined (not imported) because that map is not exported and this module must not touch shared
// files (CTO-230); the WHY note keeps the two definitions honestly in sync.
const MODEL_EXPR =
  "if(GenAiResponseModel != '', GenAiResponseModel, if(GenAiRequestModel != '', GenAiRequestModel, 'unknown'))";

/** One aggregated run as ClickHouse returns it (a whole trace reduced to the identity + cost + outcome). */
interface RunRow {
  traceId: string;
  feature: string;
  agent: string;
  model: string;
  userIdHash: string;
  tsSec: string;
  cost: string;
  maxStatus: string;
}

// The dimension filters this detector honors, bound as ClickHouse Array params (never interpolated).
// DimensionFilters (lib/filters.ts) has no `agent` axis of its own: the dashboard's dimension set is
// feature / model / layer / provider / account, and agent identity (ServiceName) is not a filterable
// dimension here. So this detector narrows by `feature` -> FeatureTag and `model` -> the model
// expression, which are the two identity dimensions of a cluster that the filter bar can drive.
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

/**
 * Query the tenant's runs over the window, cluster them by identity, and run the pure detector.
 *
 * Window: `clampWindowDays(windowDays)` days back from `now()` on the ClickHouse clock (CTO-203),
 * never the Node clock. Filters narrow the scan to the selected feature / agent / model. Returns
 * `[]` when ClickHouse is unreachable (via tryLive) and when the window simply holds no clusters:
 * there is no static or mock fallback here, a blank is the honest empty result.
 */
export async function collectDuplicatedWork(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  const found = await tryLive(async (db, tenant) => {
    const w = clampWindowDays(windowDays);
    const { clause, params } = filterClauses(filters);
    // One row per run (TraceId). A run's identity dimensions are read with any() (a run does not
    // change feature/agent/model/user mid-trace); its timestamp is the last span's, its cost the
    // trace sum, and its outcome the worst StatusCode (2 == error => failed). compute/egress spans
    // are tenant-level infra rows, not agent runs, so they are excluded exactly as queryAgents does.
    const rows = await rowsPCached<RunRow>(
      db,
      `SELECT TraceId AS traceId,
              any(FeatureTag) AS feature,
              any(ServiceName) AS agent,
              any(${MODEL_EXPR}) AS model,
              if(empty(any(UserIdHash)), '', toString(any(UserIdHash))) AS userIdHash,
              toString(toUnixTimestamp(max(Timestamp))) AS tsSec,
              sum(EstimatedCost) AS cost,
              toString(max(StatusCode)) AS maxStatus
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL ${w} DAY
         AND GenAiOperation NOT IN ('compute', 'egress')
         ${clause}
       GROUP BY TraceId`,
      { tenant, ...params },
    );

    // Group runs into clusters by the four identity dimensions. A space separator is safe: none of
    // feature/agent/model can contain one and the user hash is hex, so distinct tuples never collide.
    const byCluster = new Map<string, DuplicatedWorkCluster>();
    for (const r of rows) {
      const key = [r.feature, r.agent, r.model, r.userIdHash].join(" ");
      let cluster = byCluster.get(key);
      if (!cluster) {
        cluster = { feature: r.feature, agent: r.agent, model: r.model, userIdHash: r.userIdHash, runs: [] };
        byCluster.set(key, cluster);
      }
      cluster.runs.push({
        traceId: r.traceId,
        timestampSec: parseInt(r.tsSec, 10) || 0,
        costMicroUsd: micro(r.cost),
        outcome: parseInt(r.maxStatus, 10) === 2 ? "failed" : "success",
      });
    }

    return detectDuplicatedWork([...byCluster.values()]);
  });

  return found ?? [];
}
