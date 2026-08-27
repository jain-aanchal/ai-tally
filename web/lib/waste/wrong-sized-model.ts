// SPDX-License-Identifier: Apache-2.0
// Wrong-sized-model waste detector (CTO-231, W4; epic CTO-227; per-call basis CTO-236; per-feature
// incumbent CTO-238).
//
// WHY: the epic answers "where is this tenant paying for AI that returns nothing". One shape of that
// waste is running an expensive model on work a cheaper one handles at statistically indistinguishable
// quality. The Compare workflow (CTO-113 replay + CTO-114 pairwise-judge eval) already measures both
// halves of that claim honestly: a candidate's replay-projected cost AND its pairwise win-rate vs the
// incumbent with a Wilson 95% CI. This detector does not re-measure any of that; it reads those
// existing results and, ONLY when they clear the same judged/replayed floors the /compare route uses,
// turns a clear cost win at no significant quality regression into a WasteFinding.
//
// CTO-236 (the per-call rewrite): the prior version rescaled current spend by the ratio of the
// candidate's replay projection to the INCUMBENT's replay projection. That denominator was never
// obtainable on v1 - the gateway's /v1/replay projects only the requested candidate models, never the
// incumbent (see app.py per-candidate loop over `candidates`), so the collector returned [] every time.
// It also mis-keyed: matching a dated response-model incumbent id against a candidate base id never
// lined up. The per-call basis avoids BOTH bugs. Each replay candidate's average cost per call is
// `projected_monthly_cost_micro_usd / samples_available` (the gateway builds the projection as
// `round(avg_cost_per_call * samples_available)`, so dividing recovers the per-call figure). The
// incumbent's average cost per call is measured DIRECTLY from its own real traffic (avg EstimatedCost
// over its chat spans), never joined to a candidate id. We compare per call vs per call: apples to
// apples, no incumbent replay row required, no cross-id cost join.
//
// The honesty posture (CLAUDE.md, and the "no fake quality number" rule the /compare page already
// enforces) is load-bearing: no captured replay corpus (`samples_available <= 0`) or no judged eval
// signal (every candidate below the judged floor / null) emits NOTHING for that scope. There is
// deliberately NO mock path. A quality regression whose CI sits below the pairwise even line
// disqualifies the candidate; we never present a guessed saving over a candidate we cannot show is at
// least as good. Candidate cost is from resolved-context replay (no live retrieval); the incumbent
// cost is measured on real traffic; the two are compared per call and the reason says exactly that.
//
// CTO-238 (the per-feature incumbent): the collector used to resolve ONE tenant-wide dominant model
// (queryCurrentModel, e.g. gpt-4o) and compare every feature's replay candidates against it. But the
// replay corpus and the eval are captured PER FEATURE TAG, so this paired one feature's candidates
// against a global incumbent that may not even be the model that feature runs. The incumbent is a
// property of the feature, not the tenant: each feature's own dominant model (max spend) is the thing
// a cheaper candidate must beat. So the collector now resolves the incumbent per feature from a single
// grouped (FeatureTag, model) read, iterates the top-K features by spend, and emits one scope per
// feature with THAT feature's incumbent + window spend + measured per-call cost. queryCurrentModel is
// no longer the incumbent source here. The pure detector already supported multiple per-feature scopes,
// so only the collector changes.
//
// The pure detector (detectWrongSizedModel) is I/O-free and deterministic so it is trivially testable;
// collectWrongSizedModel does the ClickHouse + Compare-path reads and maps them through it.

import type { MicroUSD } from "@/lib/types";
import type { WasteConfidence, WasteFinding, WasteScopeKind } from "@/lib/waste";
import { clampWindowDays } from "@/lib/explore";
import type { DimensionFilters } from "@/lib/filters";
import {
  micro,
  queryEvalCandidates,
  queryReplayCandidates,
  rowsP,
  tryLive,
} from "@/lib/clickhouse";

// The pairwise-LLM-judge win-rate is candidate-vs-incumbent, so the incumbent's own quality is the
// 0.5 "even" line by construction: a candidate that wins half the judged pairs is a statistical tie
// with the incumbent. "No significant regression" is therefore "the candidate's win-rate CI still
// reaches the even line", i.e. its upper bound is at least 0.5. A CI sitting entirely below 0.5 is a
// candidate the judge finds significantly WORSE, and it is disqualified.
const PAIRWISE_EVEN = 0.5;

// Match the /compare route's floors exactly (CTO-114 MIN_JUDGED_SAMPLES, CTO-123 MIN_REPLAYED_SAMPLES):
// below these a candidate's quality / cost projection is too thin to act on, so it contributes no
// finding. Keeping the same constants means this detector and /compare agree on when a candidate
// "counts", rather than inventing a second, looser bar.
const MIN_JUDGED_SAMPLES = 10;
const MIN_REPLAYED_SAMPLES = 50;

// A Wilson 95% CI narrower than this is "tight" - a clear, well-bounded overlap with the even line,
// which we report at high confidence. A wider (but still non-regressing) CI is real signal but a
// looser one, so it lands at medium. This gates ONLY confidence, never the dollar math.
const TIGHT_CI_WIDTH = 0.1;

// The fidelity caveat every Compare projection carries (CTO diagnostics / the /compare page): the
// replay resolves captured context rather than re-running live retrieval, so a finding built on it
// must say so rather than imply a live A/B. Kept verbatim so the wording matches the page.
const REPLAY_FIDELITY_CAVEAT = "resolved-context replay, no live retrieval";

/**
 * Strip a dated / versioned suffix so two ids for the SAME model compare equal. This is used ONLY to
 * skip a candidate that is really the incumbent (self-comparison) - CTO-236 does NOT reintroduce a
 * cross-id COST join (that was the mis-keying bug). Costs are compared per call, never matched by id.
 * Examples: `claude-sonnet-4-5-20250219` -> `claude-sonnet-4-5`, `gpt-5-mini-2025-01-01` -> `gpt-5-mini`.
 */
export function baseModelId(model: string): string {
  return model
    .trim()
    .toLowerCase()
    .replace(/[-@](\d{6,8}|\d{4}-\d{2}-\d{2}|v\d+)$/i, "");
}

/**
 * One cheaper candidate for a scope, as measured by the Compare workflow, on a PER-CALL basis.
 * `perCallMicroUsd` is the candidate's average replay cost per call (derived from the gateway's
 * `projected_monthly_cost_micro_usd / samples_available`), directly comparable to the incumbent's own
 * measured per-call cost. `winRate` / `ciLow` / `ciHigh` are the pairwise-judge win-rate (0..1) vs the
 * incumbent and its Wilson 95% CI. Sample counts are the judged / replayed floors gate.
 */
export interface WrongSizedModelCandidate {
  candidateModel: string;
  provider: string;
  perCallMicroUsd: MicroUSD;
  winRate: number;
  ciLow: number;
  ciHigh: number;
  samplesJudged: number;
  samplesReplayed: number;
}

/**
 * One scope (an incumbent model, optionally within a feature) with its current windowed spend, the
 * incumbent's own MEASURED average cost per call (the rescale basis, from real traffic), and the
 * cheaper candidates the Compare workflow measured against it.
 */
export interface WrongSizedModelScope {
  scopeKind: WasteScopeKind;
  scopeValue: string;
  /** Feature label for the detail view, when the scope is narrowed to one feature. */
  feature?: string;
  incumbentModel: string;
  /** Observed spend on this scope over the window. Always known. Integer micro-USD. */
  windowSpendMicroUsd: MicroUSD;
  /** Incumbent's measured average cost per call over the window (real traffic). Integer micro-USD. */
  incumbentPerCallMicroUsd: MicroUSD;
  candidates: WrongSizedModelCandidate[];
}

export interface WrongSizedModelInput {
  windowDays: number;
  scopes: WrongSizedModelScope[];
}

/** Round to integer micro-USD. Money is never a float across the boundary. */
function microRound(n: number): number {
  return Math.round(n);
}

/**
 * Does this candidate qualify? It must be measured over both floors, be cheaper than the incumbent PER
 * CALL, AND show no significant quality regression (its win-rate CI upper bound still reaches the
 * pairwise even line). A candidate the judge finds significantly worse (ciHigh < 0.5) is rejected even
 * when it is cheaper - that is the whole "no fake saving over a worse model" rule. A candidate that is
 * really the incumbent (same base id) is skipped: comparing a model to itself is never a finding.
 */
function qualifies(
  c: WrongSizedModelCandidate,
  incumbentModel: string,
  incumbentPerCallMicroUsd: MicroUSD,
): boolean {
  // Self-comparison: the candidate IS the incumbent (dated/base id aside). Never flag a model vs itself.
  if (baseModelId(c.candidateModel) === baseModelId(incumbentModel)) return false;
  if (c.samplesJudged < MIN_JUDGED_SAMPLES) return false;
  if (c.samplesReplayed < MIN_REPLAYED_SAMPLES) return false;
  // A ratio is only defensible when the incumbent has a positive per-call cost to rescale against.
  if (incumbentPerCallMicroUsd <= 0) return false;
  if (c.perCallMicroUsd >= incumbentPerCallMicroUsd) return false;
  // No significant regression: the CI still overlaps (or sits above) the even line.
  return c.ciHigh >= PAIRWISE_EVEN;
}

/**
 * Detect wrong-sized-model waste. PURE and deterministic.
 *
 * For each scope, consider only candidates that clear both sample floors, are cheaper PER CALL than the
 * incumbent, and show no significant quality regression (see {@link qualifies}). Among those, the one
 * with the lowest per-call cost recovers the most, so it is the one we surface - one finding per scope.
 * Recoverable rescales the current windowed spend by the per-call cost ratio:
 *
 *     recoverable = round(windowSpend × (1 − candidatePerCall / incumbentPerCall))
 *
 * which is always positive here (the candidate is strictly cheaper per call). Confidence is `high` when
 * the winning candidate's CI is tight, `medium` otherwise. A scope with no qualifying candidate yields
 * NOTHING - never a zero-dollar finding.
 */
export function detectWrongSizedModel(input: WrongSizedModelInput): WasteFinding[] {
  const findings: WasteFinding[] = [];

  for (const scope of input.scopes) {
    const eligible = scope.candidates.filter((c) =>
      qualifies(c, scope.incumbentModel, scope.incumbentPerCallMicroUsd),
    );
    if (eligible.length === 0) continue;

    // Cheapest qualifying candidate per call = largest recoverable. Ties break on the higher win-rate,
    // then on the model id, so the choice is deterministic regardless of input order.
    const best = eligible.reduce((a, b) => {
      if (b.perCallMicroUsd !== a.perCallMicroUsd) {
        return b.perCallMicroUsd < a.perCallMicroUsd ? b : a;
      }
      if (b.winRate !== a.winRate) return b.winRate > a.winRate ? b : a;
      return b.candidateModel < a.candidateModel ? b : a;
    });

    const ratio = best.perCallMicroUsd / scope.incumbentPerCallMicroUsd;
    const rescaled = microRound(scope.windowSpendMicroUsd * ratio);
    const recoverableMicroUsd = Math.max(0, scope.windowSpendMicroUsd - rescaled);
    const perCallSavings = Math.max(0, scope.incumbentPerCallMicroUsd - best.perCallMicroUsd);
    const qualityDelta = best.winRate - PAIRWISE_EVEN;
    // Round the width before the threshold test so float noise (0.54 - 0.44 = 0.1000…03) does not
    // tip a genuinely tight CI over the boundary.
    const ciWidth = Math.round((best.ciHigh - best.ciLow) * 1000) / 1000;
    const confidence: WasteConfidence = ciWidth <= TIGHT_CI_WIDTH ? "high" : "medium";

    const featureClause = scope.feature ? ` on ${scope.feature}` : "";
    findings.push({
      category: "wrong_sized_model",
      scopeKind: scope.scopeKind,
      scopeValue: scope.scopeValue,
      recoverableMicroUsd,
      windowSpendMicroUsd: scope.windowSpendMicroUsd,
      confidence,
      title: `${scope.incumbentModel} is over-sized for this workload`,
      reason:
        `${best.candidateModel} replayed cheaper per call than ${scope.incumbentModel}${featureClause} ` +
        `at no significant quality regression (pairwise win-rate ${best.winRate.toFixed(2)}, 95% CI ` +
        `${best.ciLow.toFixed(2)}-${best.ciHigh.toFixed(2)} overlaps the even line). Candidate cost is ` +
        `from ${REPLAY_FIDELITY_CAVEAT} over a representative corpus; the incumbent cost is measured on ` +
        `real traffic; the two are compared per call.`,
      evidence: {
        incumbentModel: scope.incumbentModel,
        candidateModel: best.candidateModel,
        incumbentPerCall: scope.incumbentPerCallMicroUsd,
        candidatePerCall: best.perCallMicroUsd,
        perCallSavings,
        qualityDelta: Math.round(qualityDelta * 1000) / 1000,
        qualityCiLow: Math.round(best.ciLow * 1000) / 1000,
        qualityCiHigh: Math.round(best.ciHigh * 1000) / 1000,
        samplesReplayed: best.samplesReplayed,
      },
      drillHref: "/compare",
    });
  }

  return findings;
}

// The replay corpus + eval are captured per FeatureTag, so an incumbent resolved tenant-wide would be
// compared against a different feature's traffic (CTO-238). Cap the per-feature replay/eval fan-out at
// the top-K features by spend: replay/eval each burn real provider/judge spend, and the tail of tiny
// features is not where the tenant's money is. K is a named const so the bound is visible.
const MAX_FEATURES = 12;

/** One feature's incumbent: its dominant model (max spend) and that model's window spend + per-call
 *  cost, plus the feature's total spend across all models (the top-K ranking key). Integer micro-USD. */
interface FeatureIncumbent {
  feature: string;
  incumbentModel: string;
  /** The dominant model's observed spend over the window. Always known. */
  windowSpendMicroUsd: number;
  /** The dominant model's measured average cost per call over the window (real traffic). */
  incumbentPerCallMicroUsd: number;
  /** The feature's spend across ALL its models, used only to rank features for the top-K cap. */
  totalSpendMicroUsd: number;
}

/**
 * Resolve each feature's incumbent in ONE grouped ClickHouse read (CTO-238).
 *
 * The incumbent is a property of the FEATURE, not the tenant: the replay corpus and eval this detector
 * compares against are captured per feature tag, so the model a cheaper candidate must beat is the one
 * THAT feature actually runs, not the tenant's globally dominant model. We read per (FeatureTag, model)
 * `sum(EstimatedCost)` and `count()` over the same LLM-family, chat-only, clamped-window slice the rest
 * of the code uses (response-model-first id, matching EXPLORE_GROUP_EXPR.model), and for each feature
 * pick its dominant model (max spend; tie-break higher call count, then higher id, so it is
 * deterministic regardless of row order). The dominant model's spend / calls give the per-feature
 * incumbent window spend and its measured per-call cost, and the feature's total spend across models is
 * the top-K ranking key. Features with an empty FeatureTag are skipped (untagged traffic is not a
 * feature). Returns null on any ClickHouse error (honest fall-through), or [] when there is no tagged
 * chat traffic in the window.
 */
async function queryFeatureIncumbents(windowDays: number): Promise<FeatureIncumbent[] | null> {
  const w = clampWindowDays(windowDays);
  return tryLive(async (db, tenant) => {
    const out = await rowsP<{ feature: string; model: string; spend: string; calls: string | number }>(
      db,
      `SELECT FeatureTag AS feature,
              if(GenAiResponseModel != '', GenAiResponseModel,
                 if(GenAiRequestModel != '', GenAiRequestModel, 'unknown')) AS model,
              sum(EstimatedCost) AS spend,
              count() AS calls
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= toDate(now()) - INTERVAL ${w - 1} DAY
         AND GenAiOperation NOT IN ('compute', 'egress')
         AND FeatureTag != ''
       GROUP BY feature, model`,
      { tenant },
    );

    // Fold (feature, model) rows into one incumbent per feature. The dominant model is the running
    // max on (spend, calls, id); the feature total sums every model's spend for the top-K ranking.
    interface Agg {
      incumbentModel: string;
      windowSpendMicroUsd: number;
      incumbentCalls: number;
      totalSpendMicroUsd: number;
    }
    const byFeature = new Map<string, Agg>();
    for (const r of out) {
      if (!r.feature) continue; // defensive: the SQL already excludes '' FeatureTag
      const spend = micro(r.spend);
      const calls =
        typeof r.calls === "number" ? r.calls : parseInt(r.calls, 10) || 0;
      let a = byFeature.get(r.feature);
      if (!a) {
        a = { incumbentModel: r.model, windowSpendMicroUsd: spend, incumbentCalls: calls, totalSpendMicroUsd: 0 };
        byFeature.set(r.feature, a);
      }
      a.totalSpendMicroUsd += spend;
      // Dominant = max spend; tie-break on higher call count, then higher id, for a deterministic pick.
      const better =
        spend > a.windowSpendMicroUsd ||
        (spend === a.windowSpendMicroUsd &&
          (calls > a.incumbentCalls ||
            (calls === a.incumbentCalls && r.model > a.incumbentModel)));
      if (better) {
        a.incumbentModel = r.model;
        a.windowSpendMicroUsd = spend;
        a.incumbentCalls = calls;
      }
    }

    const features: FeatureIncumbent[] = [];
    for (const [feature, a] of byFeature) {
      features.push({
        feature,
        incumbentModel: a.incumbentModel,
        windowSpendMicroUsd: a.windowSpendMicroUsd,
        // Money stays integer micro-USD; the per-call average is rounded at this boundary.
        incumbentPerCallMicroUsd:
          a.incumbentCalls > 0 ? Math.round(a.windowSpendMicroUsd / a.incumbentCalls) : 0,
        totalSpendMicroUsd: a.totalSpendMicroUsd,
      });
    }
    return features;
  });
}

/**
 * Collect wrong-sized-model findings from the LIVE stack, PER FEATURE (CTO-231; per-call basis CTO-236;
 * per-feature incumbent CTO-238).
 *
 * The incumbent is resolved per feature (a single grouped read), never tenant-wide. For each in-scope
 * feature, honesty and read-frugality drive the order:
 *   1. Feature incumbent from real traffic (dominant model, its window spend, its measured per-call
 *      cost). No positive per-call cost / no traffic -> skip before any replay/eval read.
 *   2. Replay projection for THAT feature. No captured corpus (`samples_available <= 0`) -> skip.
 *   3. Eval projection for THAT feature. No candidate over the judged floor (or eval null) -> skip.
 *   4. Build the feature's per-call candidates (join replay cost + eval quality by provider+model).
 *   5. Push one scope per qualifying feature; call the pure detector ONCE over all scopes.
 *
 * Filter-driven: the window is clamped ClickHouse-clock days; a single selected `filters.feature`
 * restricts to that one feature; a non-empty `filters.model` keeps only features whose incumbent
 * (dominant) model is in the set. The fan-out is bounded to the top-{@link MAX_FEATURES} features by
 * spend. There is NO static fallback: any missing signal yields no finding for that feature, and an
 * empty result overall is the honest answer.
 */
export async function collectWrongSizedModel(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  const window = clampWindowDays(windowDays);

  // Per-feature incumbents in one grouped read. Null (ClickHouse down) or [] (no tagged traffic) both
  // mean nothing to flag.
  const features = await queryFeatureIncumbents(window);
  if (!features || features.length === 0) return [];

  // Respect the FilterBar: a single selected feature narrows to that feature; a model filter keeps only
  // features whose incumbent is one of the selected models (the model we would actually flag).
  let inScope = features;
  if (filters.feature.length === 1) {
    const only = filters.feature[0];
    inScope = inScope.filter((f) => f.feature === only);
  }
  if (filters.model.length > 0) {
    const models = new Set(filters.model);
    inScope = inScope.filter((f) => models.has(f.incumbentModel));
  }
  if (inScope.length === 0) return [];

  // Bound the replay/eval fan-out to the top-K features by spend (deterministic: spend desc, then
  // feature id asc) so per-feature Compare-path reads stay bounded.
  const ranked = [...inScope]
    .sort((a, b) =>
      b.totalSpendMicroUsd !== a.totalSpendMicroUsd
        ? b.totalSpendMicroUsd - a.totalSpendMicroUsd
        : a.feature < b.feature
          ? -1
          : 1,
    )
    .slice(0, MAX_FEATURES);

  const scopes: WrongSizedModelScope[] = [];
  for (const f of ranked) {
    // (1) No incumbent traffic to rescale against -> no finding for this feature (skip before the
    // replay/eval reads, which each burn real provider/judge spend).
    if (f.incumbentPerCallMicroUsd <= 0 || f.windowSpendMicroUsd <= 0) continue;

    // (2) Replay corpus for THIS feature. No corpus -> skip.
    const replay = await queryReplayCandidates(f.feature);
    if (!replay || replay.samples_available <= 0) continue;
    const samplesAvailable = replay.samples_available;

    // (3) Eval quality signal for THIS feature. No candidate cleared the judged floor (or eval null) ->
    // no honest quality number, skip (the same rule /compare enforces).
    const evalProj = await queryEvalCandidates(f.feature);
    if (!evalProj) continue;
    const hasJudged = evalProj.per_candidate.some((e) => e.samples_judged >= MIN_JUDGED_SAMPLES);
    if (!hasJudged) continue;

    // (4) Build per-call candidates: join replay (cost) and eval (quality) by provider+model. Each
    // candidate's per-call cost is `projected_monthly_cost_micro_usd / samples_available` (the gateway
    // built the projection as `round(avg_per_call * samples_available)`, so this recovers avg per call).
    const candidates: WrongSizedModelCandidate[] = [];
    for (const r of replay.per_candidate) {
      const e = evalProj.per_candidate.find((x) => x.provider === r.provider && x.model === r.model);
      if (!e) continue;
      candidates.push({
        candidateModel: r.model,
        provider: r.provider,
        perCallMicroUsd: Math.round(r.projected_monthly_cost_micro_usd / samplesAvailable),
        winRate: e.win_rate,
        ciLow: e.win_rate_ci_lo,
        ciHigh: e.win_rate_ci_hi,
        samplesJudged: e.samples_judged,
        samplesReplayed: r.samples_replayed,
      });
    }
    if (candidates.length === 0) continue;

    // (5) One scope per qualifying feature, carrying THAT feature's incumbent + spend + per-call basis.
    scopes.push({
      scopeKind: "feature",
      scopeValue: f.feature,
      feature: f.feature,
      incumbentModel: f.incumbentModel,
      windowSpendMicroUsd: f.windowSpendMicroUsd,
      incumbentPerCallMicroUsd: f.incumbentPerCallMicroUsd,
      candidates,
    });
  }
  if (scopes.length === 0) return [];

  // One call, all per-feature scopes: the pure detector already emits one finding per scope with the
  // right per-feature incumbent in its reason.
  return detectWrongSizedModel({ windowDays: window, scopes });
}
