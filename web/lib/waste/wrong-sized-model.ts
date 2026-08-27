// SPDX-License-Identifier: Apache-2.0
// Wrong-sized-model waste detector (CTO-231, W4; epic CTO-227; per-call basis CTO-236).
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
// The pure detector (detectWrongSizedModel) is I/O-free and deterministic so it is trivially testable;
// collectWrongSizedModel does the ClickHouse + Compare-path reads and maps them through it.

import type { MicroUSD } from "@/lib/types";
import type { WasteConfidence, WasteFinding, WasteScopeKind } from "@/lib/waste";
import { clampWindowDays } from "@/lib/explore";
import type { DimensionFilters } from "@/lib/filters";
import {
  micro,
  queryCurrentModel,
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

/**
 * The incumbent's measured average cost PER CALL and its windowed total spend, read directly from the
 * incumbent's own chat traffic (CTO-236). This is the per-call basis the candidate replay projections
 * are compared against: `avg(EstimatedCost)` over the incumbent model's chat spans (LLM-family only,
 * excluding the synthetic compute/egress rows), tenant-scoped, over the clamped ClickHouse-clock
 * window. It never touches a candidate id or a replay row, so neither prior bug (incumbent-not-in-replay
 * or dated-vs-base id mis-keying) can occur.
 *
 * The model id is resolved on the SAME response-model-first expression `queryCurrentModel` /
 * `queryCostExplore` use, so `incumbentModel` here is the id the caller already holds. Returns null on
 * any ClickHouse error (honest fall-through); a zero-count/zero-cost slice yields callCount 0, which
 * the caller treats as "no incumbent traffic to rescale" -> no finding.
 */
async function queryIncumbentPerCall(
  incumbentModel: string,
  windowDays: number,
  featureTag: string | undefined,
): Promise<{ perCallMicroUsd: number; windowSpendMicroUsd: number; callCount: number } | null> {
  const w = clampWindowDays(windowDays);
  return tryLive(async (db, tenant) => {
    const tagClause = featureTag ? "AND FeatureTag = {feature:String}" : "";
    // Resolve the incumbent on the response-model-first expression (matches EXPLORE_GROUP_EXPR.model),
    // filter to that model's chat spans over the window, and read total spend + call count so the
    // per-call average and the window spend come from ONE tenant-scoped read of the same slice.
    const out = await rowsP<{ spend: string; calls: string | number }>(
      db,
      `SELECT sum(EstimatedCost) AS spend, count() AS calls
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= toDate(now()) - INTERVAL ${w - 1} DAY
         AND GenAiOperation NOT IN ('compute', 'egress')
         AND if(GenAiResponseModel != '', GenAiResponseModel,
                if(GenAiRequestModel != '', GenAiRequestModel, 'unknown')) = {model:String}
         ${tagClause}`,
      { tenant, model: incumbentModel, ...(featureTag ? { feature: featureTag } : {}) },
    );
    const row = out[0];
    const calls =
      row == null ? 0 : typeof row.calls === "number" ? row.calls : parseInt(row.calls, 10) || 0;
    const windowSpendMicroUsd = row == null ? 0 : micro(row.spend);
    // Money stays integer micro-USD; the per-call average is rounded at this boundary.
    const perCallMicroUsd = calls > 0 ? Math.round(windowSpendMicroUsd / calls) : 0;
    return { perCallMicroUsd, windowSpendMicroUsd, callCount: calls };
  });
}

/**
 * Collect wrong-sized-model findings from the LIVE stack (CTO-231; per-call basis CTO-236).
 *
 * Order matters for honesty and for not doing pointless reads:
 *   1. Replay projection first. No captured corpus (`samples_available <= 0`) -> [] before any other
 *      read. No corpus means no candidate cost, so there is nothing to compute and nothing to fabricate.
 *   2. Eval projection. If no candidate has a judged win-rate (all below the floor / null) there is no
 *      quality signal, and the "/compare no fake quality number" rule says we emit nothing.
 *   3. Incumbent identity + its MEASURED per-call cost from real traffic, plus its window spend.
 *   4. Map to the pure detector on the per-call basis.
 *
 * Filter-driven: the window is clamped ClickHouse-clock days, a single selected `filters.feature`
 * becomes the compare tag and scopes the incumbent read, and `filters.model` restricts which incumbent
 * model we will flag. There is NO static fallback: any missing signal returns [], the honest answer.
 */
export async function collectWrongSizedModel(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  const window = clampWindowDays(windowDays);

  // The Compare projection is per-feature-tag. Only a single selected feature maps cleanly onto one
  // tag; zero or several features means "all traffic" (undefined tag), matching how /compare scopes.
  const featureTag = filters.feature.length === 1 ? filters.feature[0] : undefined;

  // (1) Replay FIRST. No corpus -> [] before we issue any other read (CTO-236: honest, no fabrication,
  // and no wasted reads when there is nothing to compare against).
  const replay = await queryReplayCandidates(featureTag);
  if (!replay || replay.samples_available <= 0) return [];
  const samplesAvailable = replay.samples_available;

  // (2) Eval quality signal. If NO candidate cleared the judged floor (or eval is null), there is no
  // honest quality number, so we flag nothing - the same rule /compare enforces.
  const evalProj = await queryEvalCandidates(featureTag);
  if (!evalProj) return [];
  const hasJudged = evalProj.per_candidate.some((e) => e.samples_judged >= MIN_JUDGED_SAMPLES);
  if (!hasJudged) return [];

  // (3) Incumbent identity from real traffic (response-model-first, matching the cost breakdown).
  const live = await queryCurrentModel();
  if (!live) return [];
  const incumbentModel = live.model;

  // If the caller filtered to specific models and the incumbent is not among them, nothing to flag.
  if (filters.model.length > 0 && !filters.model.includes(incumbentModel)) return [];

  // The incumbent's MEASURED per-call cost and window spend over the same slice. This is the per-call
  // rescale basis - no incumbent replay row and no cross-id cost join (CTO-227 review's two bugs).
  const incumbent = await queryIncumbentPerCall(incumbentModel, window, featureTag);
  if (!incumbent || incumbent.callCount <= 0 || incumbent.perCallMicroUsd <= 0) return [];
  if (incumbent.windowSpendMicroUsd <= 0) return [];

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
  if (candidates.length === 0) return [];

  return detectWrongSizedModel({
    windowDays: window,
    scopes: [
      {
        scopeKind: featureTag ? "feature" : "model",
        scopeValue: featureTag ?? incumbentModel,
        feature: featureTag,
        incumbentModel,
        windowSpendMicroUsd: incumbent.windowSpendMicroUsd,
        incumbentPerCallMicroUsd: incumbent.perCallMicroUsd,
        candidates,
      },
    ],
  });
}
