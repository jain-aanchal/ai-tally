// SPDX-License-Identifier: Apache-2.0
// Types + fallback mock for the Cross-provider Compare workflow. Real candidate data comes
// from /v1/replay (CTO-113) for cost/latency/error and from /v1/eval (CTO-114) for
// qualityScore. This module's `comparison` value is the rescaled-mock fallback used by the
// /api/compare route when the gateway has no opted-in samples yet (or is unreachable). The
// CandidateMetrics / Comparison types are the wire shape for both branches.
//
// CTO-168: the fixture's `workload` string and `recommendation` prose below are NO LONGER shipped
// on the live path. When queryCurrentModel returns real traffic, the /api/compare route derives
// `workload` from the real query context (tag filter + time window — see deriveWorkload) and
// generates `recommendation.verdict` + `summary` from the REAL computed deltas (cost savings %,
// pairwise-judge quality, latency — see deriveRecommendation). The fixture verdict/summary/workload
// survive ONLY on the unreachable-gateway fallback (queryCurrentModel === null), which the page
// labels via SyntheticPreviewBanner. This stops the old bug where the hardcoded
// "$12.2K/mo … haiku-4.5" prose and "research_agent / … / last 7 days" label rendered on live data.
//
// CTO-115: the `current` row's `latencyP95Ms` and `errorRate` are now derived from live
// otel_spans over the same 7-day window the cost query uses (see queryCurrentModel in
// clickhouse.ts). They carry `null` when the live window has fewer than 50 spans, and the
// page renders "—" in that case.
//
// CTO-123: candidate rows' `latencyP95Ms` / `errorRate` are now also grounded in real
// per-candidate replay (p95 latency + error rate over the replayed responses), with the same
// honest-null floor (null below 50 replayed responses → "—"). The numeric values on the mock
// candidates below are used only by the unreachable-gateway rescaled-mock fallback path.
//
// CTO-166: the `gemini-3-flash` / `provider: "google"` candidate below is now ALSO grounded in
// the live path — CTO-149 made Google a first-class priced provider, and gemini was added to the
// gateway candidate list (DEFAULT_CANDIDATES in clickhouse.ts) so it flows through /v1/replay +
// /v1/eval exactly like the anthropic/openai rows. The same honest-null floors apply to it:
// null latency/error below 50 replayed responses, null qualityScore below 10 judged samples —
// never a fabricated Gemini number. The numeric mock below is now purely the
// unreachable-gateway fallback, same status as the other mock candidates.
//
// CTO-114: `qualityScore` is now `number | null`. Non-null only when a pairwise-LLM-judge
// eval pass has run and judged >= 10 samples for that candidate; the value is the
// candidate's win-rate (candidate_wins / non-error judgments) with a Wilson 95% CI in
// `qualityCi`. When no eval has run, or n < 10, the route returns `null` and the page
// renders "—" rather than ever fabricating a quality number.
//
// The numeric mocks in `comparison.current` below are the unreachable-gateway fallback (CI /
// fresh clones); that path keeps showing numbers, not nulls, for backwards compatibility.

import { formatUSD, type MicroUSD } from "./types";

export interface CandidateMetrics {
  /** display label, e.g. "claude-haiku-4.5" */
  model: string;
  provider: string;
  /** projected monthly cost at current traffic */
  monthlyCostMicroUsd: MicroUSD;
  /**
   * Pairwise-LLM-judge win rate (0..1) from CTO-114. `null` when no eval pass has judged
   * >= 10 samples for this candidate — the page renders "—" in that case. NEVER substitute a
   * mock when this is null; the ticket is explicit about that. Always `null` on the `current`
   * row (no judge pair when comparing a model to itself).
   */
  qualityScore: number | null;
  /** Wilson 95% CI on the win-rate (CTO-114). Present only when `qualityScore` is a number. */
  qualityCi?: { lo: number; hi: number };
  /**
   * p95 latency in milliseconds. `null` on the `current` row when the live 7-day window has
   * fewer than 50 spans, and on candidate rows when fewer than 50 responses were replayed
   * (rendered as "—" — CTO-115 / CTO-123).
   */
  latencyP95Ms: number | null;
  /** 0..1. `null` on the `current` row under the same low-sample suppression rule (CTO-115). */
  errorRate: number | null;
}

export interface Comparison {
  workload: string;        // e.g. "research_agent / production / last 7 days"
  current: CandidateMetrics;
  candidates: CandidateMetrics[];
  /** human-written-ish recommendation (the routing rule export hooks off this) */
  recommendation: {
    verdict: "switch" | "keep" | "mixed";
    summary: string;
    projectedSavingsMicroUsd: MicroUSD;
    projectedSavingsPct: number; // 0..1
  };
  diagnostics: {
    samplesReplayed: number;
    samplesAvailable: number;
    excludedRateLimited: number;
    replayCostMicroUsd: MicroUSD;
    contextFidelity: "resolved-context replay (no live retrieval)" | "live retrieval";
    /**
     * Minutes since the reconciler last trued-up the baseline traffic this comparison is built
     * from. A projection off a stale baseline must not be presented as fresh (CTO-80).
     */
    reconcilerLastRunMinutesAgo: number;
  };
}

export function deltaPct(current: number, candidate: number): number {
  if (current === 0) return 0;
  return (candidate - current) / current;
}

// Compare fixture for the research_agent workload — the dominant cost driver from cost.ts
// ($19.1K LLM spend on this workload alone over 30 days, ≈ $4.5K/week).
//
// CTO-168: this whole object is the unreachable-gateway fallback ONLY. The `workload` label and
// the `recommendation` verdict/summary below are fixture prose — on the live path the route
// replaces them with values derived from real traffic (deriveWorkload / deriveRecommendation).
export const comparison: Comparison = {
  // Fixture label — used only in the unreachable-gateway fallback. Live path calls deriveWorkload.
  workload: "research_agent / production / last 7 days",
  current: {
    model: "claude-sonnet-4.5",
    provider: "anthropic",
    monthlyCostMicroUsd: 19_100_000_000, // $19,100/mo on this workload
    qualityScore: 0.941,
    qualityCi: { lo: 0.911, hi: 0.962 },
    latencyP95Ms: 2400,
    errorRate: 0.004,
  },
  // No qualityCi on candidates: a CI implies real eval data, which the mock-fallback path
  // doesn't have. The /api/compare route nulls qualityScore on these in the fallback path too
  // (CTO-114: never fabricate a quality number); the values here document the ideal shape only.
  candidates: [
    {
      model: "claude-haiku-4.5",
      provider: "anthropic",
      monthlyCostMicroUsd: 5_300_000_000, // ~72% cheaper than current
      qualityScore: 0.908,
      latencyP95Ms: 1800,
      errorRate: 0.006,
    },
    {
      model: "gpt-5-mini",
      provider: "openai",
      monthlyCostMicroUsd: 6_250_000_000, // ~67% cheaper
      qualityScore: 0.894,
      latencyP95Ms: 1600,
      errorRate: 0.009,
    },
    {
      model: "gemini-3-flash",
      provider: "google",
      monthlyCostMicroUsd: 4_490_000_000, // ~76% cheaper
      qualityScore: 0.871,
      latencyP95Ms: 1400,
      errorRate: 0.012,
    },
  ],
  // Fixture verdict/summary — unreachable-gateway fallback ONLY. The live path never renders this
  // prose; the route calls deriveRecommendation off the real computed deltas instead (CTO-168).
  recommendation: {
    verdict: "mixed",
    summary:
      "Route short prompts (<1k tokens) to haiku-4.5; keep current for long-context (>4k tokens). Projected quality delta -1.1pp at the projected mix, saves ~$12.2K/mo on this workload.",
    projectedSavingsMicroUsd: 12_200_000_000,
    projectedSavingsPct: 0.64,
  },
  diagnostics: {
    samplesReplayed: 4200,
    samplesAvailable: 87_400,
    excludedRateLimited: 312,
    replayCostMicroUsd: 42_300_000, // $42.30 spent replaying = ~0.2% of monthly workload spend
    contextFidelity: "resolved-context replay (no live retrieval)",
    reconcilerLastRunMinutesAgo: 18,
  },
};

// --- CTO-168: live-path grounding for the workload label + recommendation prose ------------------
//
// These replace the fixture strings whenever the /api/compare route has real traffic to work from.
// They are pure (no I/O) so they're unit-testable and shared across the route's live branches.

/**
 * Human-readable label for the workload a comparison was built from, derived from the real query
 * context: the `?tag=` feature filter (or "all traffic" when unfiltered) and the current-model
 * cost window (queryCurrentModel reads the last 7 days). Replaces the fixture
 * "research_agent / production / last 7 days" on the live path.
 */
export function deriveWorkload(featureTag: string | undefined, windowDays: number): string {
  const scope = featureTag && featureTag.trim() ? featureTag.trim() : "all traffic";
  return `${scope} / production / last ${windowDays} days`;
}

/**
 * Minimum replayed-response count across all candidates before we'll issue a switch/keep verdict.
 * Below this the projection is too thin to recommend anything — we return an honest "insufficient
 * data" summary instead of the fixture prose. Matches the per-candidate replay floor (CTO-123).
 */
export const MIN_SAMPLES_TO_RECOMMEND = 50;

export interface RecommendationCandidate {
  model: string;
  monthlyCostMicroUsd: MicroUSD;
  /** Pairwise-LLM-judge win-rate (0..1) vs current, or null when < 10 judged samples (CTO-114). */
  qualityScore: number | null;
  /** p95 latency in ms, or null below the replay floor (CTO-123). */
  latencyP95Ms: number | null;
}

export interface DerivedRecommendation {
  verdict: "switch" | "keep" | "mixed";
  summary: string;
  projectedSavingsMicroUsd: MicroUSD;
  projectedSavingsPct: number;
}

// Below this fractional cost saving we don't consider a switch worthwhile (noise, not signal).
const MIN_MEANINGFUL_SAVINGS_PCT = 0.05;

/**
 * Generate a data-driven recommendation (verdict + templated summary + projected savings) from the
 * REAL computed deltas of a live comparison. Never emits the fixture prose.
 *
 * The savings are always computed off the cheapest candidate vs the live current cost. The verdict:
 *   - thin data (no candidates, or `samplesReplayed` below the floor) → honest "insufficient data".
 *   - cheapest saves < 5% → "keep" (current is already near-cheapest).
 *   - meaningful savings + candidate wins ≥ 50% of judged pairs → "switch".
 *   - meaningful savings + candidate wins < 50% → "mixed" (cost win, quality regression).
 *   - meaningful savings + quality not yet judged → "mixed" (run an eval before switching).
 */
export function deriveRecommendation(input: {
  currentModel: string;
  currentCostMicroUsd: MicroUSD;
  candidates: RecommendationCandidate[];
  /** total replayed responses across candidates — the gate for whether we recommend at all. */
  samplesReplayed: number;
}): DerivedRecommendation {
  const { currentModel, currentCostMicroUsd, candidates, samplesReplayed } = input;

  const cheapest = candidates.reduce<RecommendationCandidate | null>(
    (best, c) => (best === null || c.monthlyCostMicroUsd < best.monthlyCostMicroUsd ? c : best),
    null,
  );

  const projectedSavingsMicroUsd = cheapest
    ? Math.max(0, currentCostMicroUsd - cheapest.monthlyCostMicroUsd)
    : 0;
  const projectedSavingsPct =
    currentCostMicroUsd > 0 ? projectedSavingsMicroUsd / currentCostMicroUsd : 0;

  // Thin / absent data — say so honestly rather than shipping a confident sentence off noise.
  if (!cheapest || samplesReplayed < MIN_SAMPLES_TO_RECOMMEND) {
    return {
      verdict: "mixed",
      summary: cheapest
        ? `— insufficient replay data to recommend a switch (only ${samplesReplayed.toLocaleString()} of the needed ${MIN_SAMPLES_TO_RECOMMEND} responses replayed). Run a fuller replay pass.`
        : `— no alternative candidate cleared replay for this workload yet. Keep ${currentModel} until a candidate has samples.`,
      projectedSavingsMicroUsd,
      projectedSavingsPct,
    };
  }

  const pct = Math.round(projectedSavingsPct * 100);
  const dollars = formatUSD(projectedSavingsMicroUsd);
  const latencyClause =
    cheapest.latencyP95Ms !== null ? ` Latency p95 ${cheapest.latencyP95Ms}ms.` : "";

  // Current is already at/near the cheapest option — no candidate saves enough to bother switching.
  if (projectedSavingsPct < MIN_MEANINGFUL_SAVINGS_PCT) {
    return {
      verdict: "keep",
      summary: `Keep ${currentModel}: the cheapest candidate (${cheapest.model}) saves only ${pct}% — below the ${Math.round(
        MIN_MEANINGFUL_SAVINGS_PCT * 100,
      )}% threshold worth a switch.`,
      projectedSavingsMicroUsd,
      projectedSavingsPct,
    };
  }

  // Meaningful savings — the verdict now hinges on quality (the pairwise-judge win-rate vs current).
  if (cheapest.qualityScore === null) {
    return {
      verdict: "mixed",
      summary: `${cheapest.model} projects ${pct}% cheaper (saves ${dollars}/mo vs ${currentModel}), but no eval has judged its quality yet — run an eval pass before routing production traffic.${latencyClause}`,
      projectedSavingsMicroUsd,
      projectedSavingsPct,
    };
  }

  const winPct = Math.round(cheapest.qualityScore * 100);
  if (cheapest.qualityScore >= 0.5) {
    return {
      verdict: "switch",
      summary: `Switch to ${cheapest.model}: ${pct}% cheaper (saves ${dollars}/mo) and wins ${winPct}% of judged pairs against ${currentModel}.${latencyClause}`,
      projectedSavingsMicroUsd,
      projectedSavingsPct,
    };
  }

  return {
    verdict: "mixed",
    summary: `${cheapest.model} is ${pct}% cheaper (saves ${dollars}/mo) but wins only ${winPct}% of judged pairs vs ${currentModel} — route cost-tolerant traffic to it, keep ${currentModel} for quality-critical calls.${latencyClause}`,
    projectedSavingsMicroUsd,
    projectedSavingsPct,
  };
}
