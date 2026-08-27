// SPDX-License-Identifier: Apache-2.0
// Wrong-sized-model waste detector (CTO-231, W4; epic CTO-227).
//
// WHY: the epic answers "where is this tenant paying for AI that returns nothing". One shape of that
// waste is running an expensive model on work a cheaper one handles at statistically indistinguishable
// quality. The Compare workflow (CTO-113 replay + CTO-114 pairwise-judge eval) already measures both
// halves of that claim honestly: a cheaper candidate's replay-projected cost AND its pairwise win-rate
// vs the incumbent with a Wilson 95% CI. This detector does not re-measure anything; it reads those
// existing results and, ONLY when they clear the same judged/replayed floors the /compare route uses,
// turns a clear cost win at no significant quality regression into a WasteFinding.
//
// The honesty posture (CLAUDE.md, and the "no fake quality number" rule the /compare page already
// enforces) is load-bearing: if there is no eval pass (below the judged floor) or no replay for the
// tenant, this detector emits NOTHING for that scope. There is deliberately NO mock path. A quality
// regression whose CI sits below the pairwise even line disqualifies the candidate; we never present a
// guessed saving over a candidate we cannot show is at least as good.
//
// The pure detector (detectWrongSizedModel) is I/O-free and deterministic so it is trivially testable;
// collectWrongSizedModel does the ClickHouse + Compare-path reads and maps them through it.

import type { MicroUSD } from "@/lib/types";
import type { WasteConfidence, WasteFinding, WasteScopeKind } from "@/lib/waste";
import { clampWindowDays } from "@/lib/explore";
import type { DimensionFilters } from "@/lib/filters";
import {
  queryCostExplore,
  queryCurrentModel,
  queryEvalCandidates,
  queryReplayCandidates,
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

// A Wilson 95% CI narrower than this is "tight" — a clear, well-bounded overlap with the even line,
// which we report at high confidence. A wider (but still non-regressing) CI is real signal but a
// looser one, so it lands at medium. This gates ONLY confidence, never the dollar math.
const TIGHT_CI_WIDTH = 0.1;

// The fidelity caveat every Compare projection carries (CTO diagnostics / the /compare page): the
// replay resolves captured context rather than re-running live retrieval, so a finding built on it
// must say so rather than imply a live A/B. Kept verbatim so the wording matches the page.
const REPLAY_FIDELITY_CAVEAT = "resolved-context replay, no live retrieval";

/**
 * One cheaper candidate for a scope, as measured by the Compare workflow. `projectedMonthlyMicroUsd`
 * is the candidate's replay-projected monthly cost over the SAME corpus the incumbent projection uses,
 * so its ratio to the incumbent's projection is the per-call cost ratio we rescale current spend by.
 * `winRate` / `ciLow` / `ciHigh` are the pairwise-judge win-rate (0..1) vs the incumbent and its
 * Wilson 95% CI. Sample counts are the judged / replayed floors gate.
 */
export interface WrongSizedModelCandidate {
  candidateModel: string;
  provider: string;
  projectedMonthlyMicroUsd: MicroUSD;
  winRate: number;
  ciLow: number;
  ciHigh: number;
  samplesJudged: number;
  samplesReplayed: number;
}

/**
 * One scope (an incumbent model, optionally within a feature) with its current windowed spend, the
 * incumbent's own replay-projected monthly cost (the rescale denominator), and the cheaper candidates
 * the Compare workflow measured against it.
 */
export interface WrongSizedModelScope {
  scopeKind: WasteScopeKind;
  scopeValue: string;
  /** Feature label for the detail view, when the scope is narrowed to one feature. */
  feature?: string;
  incumbentModel: string;
  /** Observed spend on this scope over the window. Always known. Integer micro-USD. */
  windowSpendMicroUsd: MicroUSD;
  /** Incumbent's replay-projected monthly cost over the same corpus as the candidates. */
  incumbentProjectedMonthlyMicroUsd: MicroUSD;
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
 * Does this candidate qualify? It must be measured over both floors, be cheaper than the incumbent on
 * the replay projection, AND show no significant quality regression (its win-rate CI upper bound still
 * reaches the pairwise even line). A candidate the judge finds significantly worse (ciHigh < 0.5) is
 * rejected even when it is cheaper — that is the whole "no fake saving over a worse model" rule.
 */
function qualifies(c: WrongSizedModelCandidate, incumbentProjectedMonthlyMicroUsd: MicroUSD): boolean {
  if (c.samplesJudged < MIN_JUDGED_SAMPLES) return false;
  if (c.samplesReplayed < MIN_REPLAYED_SAMPLES) return false;
  // A ratio is only defensible when the incumbent has a positive projection to rescale against.
  if (incumbentProjectedMonthlyMicroUsd <= 0) return false;
  if (c.projectedMonthlyMicroUsd >= incumbentProjectedMonthlyMicroUsd) return false;
  // No significant regression: the CI still overlaps (or sits above) the even line.
  return c.ciHigh >= PAIRWISE_EVEN;
}

/**
 * Detect wrong-sized-model waste. PURE and deterministic.
 *
 * For each scope, consider only candidates that clear both sample floors, are cheaper on the replay
 * projection, and show no significant quality regression (see {@link qualifies}). Among those, the one
 * with the lowest projected cost recovers the most, so it is the one we surface — one finding per
 * scope. Recoverable is the current windowed spend minus that spend rescaled to the candidate's
 * per-call replay cost:
 *
 *     recoverable = windowSpend - round(windowSpend × candidateProjected / incumbentProjected)
 *
 * which is always positive here (the candidate is strictly cheaper). Confidence is `high` when the
 * winning candidate's CI is tight, `medium` otherwise. A scope with no qualifying candidate yields
 * NOTHING — never a zero-dollar finding.
 */
export function detectWrongSizedModel(input: WrongSizedModelInput): WasteFinding[] {
  const findings: WasteFinding[] = [];

  for (const scope of input.scopes) {
    const eligible = scope.candidates.filter((c) =>
      qualifies(c, scope.incumbentProjectedMonthlyMicroUsd),
    );
    if (eligible.length === 0) continue;

    // Cheapest qualifying candidate = largest recoverable. Ties break on the higher win-rate, then on
    // the model id, so the choice is deterministic regardless of input order.
    const best = eligible.reduce((a, b) => {
      if (b.projectedMonthlyMicroUsd !== a.projectedMonthlyMicroUsd) {
        return b.projectedMonthlyMicroUsd < a.projectedMonthlyMicroUsd ? b : a;
      }
      if (b.winRate !== a.winRate) return b.winRate > a.winRate ? b : a;
      return b.candidateModel < a.candidateModel ? b : a;
    });

    const ratio = best.projectedMonthlyMicroUsd / scope.incumbentProjectedMonthlyMicroUsd;
    const rescaled = microRound(scope.windowSpendMicroUsd * ratio);
    const recoverableMicroUsd = Math.max(0, scope.windowSpendMicroUsd - rescaled);
    const projectedMonthlySavings = Math.max(
      0,
      scope.incumbentProjectedMonthlyMicroUsd - best.projectedMonthlyMicroUsd,
    );
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
        `${best.candidateModel} replayed cheaper than ${scope.incumbentModel}${featureClause} at no ` +
        `significant quality regression (pairwise win-rate ${best.winRate.toFixed(2)}, 95% CI ` +
        `${best.ciLow.toFixed(2)}-${best.ciHigh.toFixed(2)} overlaps the even line). Figures are from ` +
        `${REPLAY_FIDELITY_CAVEAT}.`,
      evidence: {
        incumbentModel: scope.incumbentModel,
        candidateModel: best.candidateModel,
        qualityDelta: Math.round(qualityDelta * 1000) / 1000,
        qualityCiLow: Math.round(best.ciLow * 1000) / 1000,
        qualityCiHigh: Math.round(best.ciHigh * 1000) / 1000,
        projectedMonthlySavings,
      },
      drillHref: "/compare",
    });
  }

  return findings;
}

/**
 * The model id ClickHouse attributes spend to, matching the resolution `queryCostExplore`'s `model`
 * dimension and `queryCurrentModel` both use (response model when present, request model otherwise).
 * CTO-227 review finding (Bug 4): `queryCurrentModel` now resolves the incumbent on that SAME
 * response-model-first expression (previously it preferred `SpanAttributes['chatbot.real_model']`,
 * which never matched a cost-breakdown group on the chatbot demo, so this detector always returned []).
 * With both sides aligned, we compare the id directly.
 */
function resolveIncumbentModel(model: string): string {
  return model;
}

/**
 * Collect wrong-sized-model findings from the LIVE stack (CTO-231).
 *
 * This reuses the EXISTING Compare workflow reads — the cached `/v1/replay` (CTO-113) and `/v1/eval`
 * (CTO-114) projections behind the /compare page, plus the live current model — rather than re-running
 * replay or eval. It joins them to the windowed cost-by-model read (`queryCostExplore` grouped by
 * model) so the incumbent's window spend, the rescale denominator, and the candidates all describe the
 * same tenant slice, then maps through {@link detectWrongSizedModel}.
 *
 * Filter-driven: the window is clamped ClickHouse-clock days, `filters.feature` narrows the Compare
 * projection (a single selected feature becomes the compare tag) and the cost read, and `filters.model`
 * restricts which incumbent model we will flag. There is NO static fallback: if replay, eval, the
 * current model, or the cost read is unavailable — as on a demo tenant with no eval pass — this returns
 * `[]`, which is the honest answer, not a failure.
 *
 * CTO-227 review pass 2, inert on v1: even with every read present, this returns `[]` in production
 * today. v1 `/v1/replay` projects only the requested candidate models, never the incumbent, so the
 * rescale denominator (the incumbent's replay-corpus projection) cannot be formed and the per-call
 * rescale is not computable. The honest per-call-cost approach is CTO-236; see the denominator lookup
 * below for the full explanation. This is documented, intended behaviour, not a masked dead path.
 */
export async function collectWrongSizedModel(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  // CTO-227 review pass 3 (Fix 3): gate this collector OFF before it issues any read. As the
  // honest-state note further down documents, v1 `/v1/replay` does not project the incumbent, so the
  // rescale denominator can never be formed and this collector already returned [] EVERY time in
  // production. Issuing its four ClickHouse/Compare reads (queryReplayCandidates, queryEvalCandidates,
  // queryCurrentModel, queryCostExplore) on every dashboard load only to reach that guaranteed [] is
  // pure waste, so we short-circuit here and do zero reads today. Re-enable when CTO-236 lands the
  // incumbent replay projection (or a per-call-cost rescale that needs no incumbent replay row): the
  // live-read body below is kept intact as the wiring reference for that work, and the pure
  // `detectWrongSizedModel` above stays correct for a valid input. The flag is a plain boolean, so the
  // retained body still type-checks and its imports stay used.
  const CTO236_LANDED = false;
  if (!CTO236_LANDED) return [];

  const window = clampWindowDays(windowDays);

  // The Compare projection is per-feature-tag. Only a single selected feature maps cleanly onto one
  // tag; zero or several features means "all traffic" (undefined tag), matching how /compare scopes.
  const featureTag = filters.feature.length === 1 ? filters.feature[0] : undefined;

  // Reuse the cached Compare-path reads. Any null means the signal we require is unavailable; there is
  // no mock path, so we return [] rather than guess.
  const [replay, evalProj, live] = await Promise.all([
    queryReplayCandidates(featureTag),
    queryEvalCandidates(featureTag),
    queryCurrentModel(),
  ]);
  if (!replay || !evalProj || !live) return [];

  const incumbentModel = resolveIncumbentModel(live.model);

  // If the caller filtered to specific models and the incumbent is not among them, there is nothing to
  // flag for this slice.
  if (filters.model.length > 0 && !filters.model.includes(incumbentModel)) return [];

  // Windowed cost-by-model over the same slice, so the incumbent's window spend comes from real
  // telemetry rather than the 7-day→30-day projection the Compare current-cost uses.
  const costByModel = await queryCostExplore({
    window: { kind: "preset", days: window },
    groupBy: "model",
    filters: {
      ...(filters.feature.length > 0 ? { feature: filters.feature } : {}),
      ...(filters.model.length > 0 ? { model: filters.model } : {}),
    },
  });
  if (!costByModel) return [];

  const incumbentRow = costByModel.breakdown.find((r) => r.group === incumbentModel);
  // No observed spend on the incumbent in this window → nothing to recover, so nothing to report.
  if (!incumbentRow || incumbentRow.totalMicroUsd <= 0) return [];

  // The rescale denominator has to be the incumbent's projection on the SAME replay corpus every
  // candidate's `projected_monthly_cost_micro_usd` is measured on (CTO-227 review Bug 1: the old
  // `live.monthlyCostMicroUsd` denominator was a 7-day-traffic figure, an incompatible basis that made
  // the "cheaper" gate and the recoverable ratio meaningless).
  //
  // CTO-227 review pass 2 (HONEST-STATE NOTE): v1 `/v1/replay` does NOT project the incumbent. The
  // gateway builds `per_candidate` ONLY from the requested `candidate_models` (see app.py: the
  // per-candidate loop iterates `candidates`), and `queryReplayCandidates` sends just DEFAULT_CANDIDATES
  // (claude-haiku-4-5, gpt-5-mini, gemini-3-flash), never the incumbent. So for a real incumbent this
  // `find` is ALWAYS undefined and the collector returns [] here EVERY time in production: an
  // apples-to-apples per-call rescale is simply not computable on v1. This is deliberate honesty, not a
  // latent bug; we never fabricate a denominator from an incompatible basis. The real implementation
  // (an incumbent replay projection, or a per-call-cost rescale that needs no incumbent replay row)
  // lands in CTO-236; until then this detector is inert by design. The pure `detectWrongSizedModel`
  // above stays correct FOR A VALID INPUT, but no such input can be produced on v1.
  const incumbentReplay = replay.per_candidate.find((r) => r.model === incumbentModel);
  if (!incumbentReplay || incumbentReplay.projected_monthly_cost_micro_usd <= 0) return [];
  const incumbentProjectedMonthlyMicroUsd = incumbentReplay.projected_monthly_cost_micro_usd;

  // Join replay (cost) and eval (quality) by provider+model, excluding the incumbent itself.
  const candidates: WrongSizedModelCandidate[] = [];
  for (const r of replay.per_candidate) {
    if (r.model === incumbentModel) continue;
    const e = evalProj.per_candidate.find((x) => x.provider === r.provider && x.model === r.model);
    if (!e) continue;
    candidates.push({
      candidateModel: r.model,
      provider: r.provider,
      projectedMonthlyMicroUsd: r.projected_monthly_cost_micro_usd,
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
        scopeKind: "model",
        scopeValue: incumbentModel,
        feature: featureTag,
        incumbentModel,
        windowSpendMicroUsd: incumbentRow.totalMicroUsd,
        // Replay-corpus projection for the incumbent — shares the candidates' basis (see above).
        incumbentProjectedMonthlyMicroUsd,
        candidates,
      },
    ],
  });
}
