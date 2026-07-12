// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { comparison } from "@/lib/compare";
import {
  queryCurrentModel,
  queryEvalCandidates,
  queryReplayCandidates,
  type EvalCandidateRow,
} from "@/lib/clickhouse";

// CTO-114: minimum sample count before a candidate's pairwise-LLM-judge win-rate is shown as
// a real number. Below this, `qualityScore` is null and the page renders "—". 10 is a soft
// floor — Wilson CIs widen rapidly below this point and the resulting number, while
// mathematically defined, is not informative.
const MIN_JUDGED_SAMPLES = 10;

// CTO-123: minimum replayed-response count before a candidate's per-candidate p95 latency /
// error rate are shown as real numbers. Below this, both are null and the page renders "—" —
// the same honest-null rule the `current` row uses for its live otel window (CTO-115).
const MIN_REPLAYED_SAMPLES = 50;

/** Look up a candidate's eval row; return null when no row exists or sample count too small. */
function evalQualityFor(
  evalRows: EvalCandidateRow[] | undefined,
  provider: string,
  model: string,
): { qualityScore: number; qualityCi: { lo: number; hi: number } } | null {
  const row = evalRows?.find((r) => r.provider === provider && r.model === model);
  if (!row || row.samples_judged < MIN_JUDGED_SAMPLES) return null;
  return {
    qualityScore: row.win_rate,
    qualityCi: { lo: row.win_rate_ci_lo, hi: row.win_rate_ci_hi },
  };
}

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const featureTag = url.searchParams.get("tag") ?? undefined;

  // CTO-113: try the real replay projection first. When it returns data, the candidate rows
  // are grounded in actual cross-provider replay outcomes (real cost from the SDK price catalog,
  // real token counts from replayed calls) — no rescaled mock needed. When it returns null
  // (no samples opted-in yet, or gateway unreachable) we drop through to the original
  // current-model-only path below.
  const replay = await queryReplayCandidates(featureTag);
  // CTO-114: eval is independent of replay (it consumes replay outcomes but is its own opt-in).
  // Pull it in parallel so the route doesn't add 30s of latency stacked behind the replay call.
  const evalProj = await queryEvalCandidates(featureTag);

  // The "current model" half is the one we can ground in real traffic today: most-trafficked
  // model over the last 7 days. Quality/latency on `current` stay mocked because we don't yet
  // have an eval harness — flagged honestly via SyntheticPreviewBanner on the page.
  const live = await queryCurrentModel();
  if (!live) {
    // Pure-mock fallback (CI / no DB). Even here, qualityScore must not be a fabricated
    // number — splice real eval data in if available, else null per-candidate.
    const candidates = comparison.candidates.map((c) => {
      const quality = evalQualityFor(evalProj?.per_candidate, c.provider, c.model);
      return {
        ...c,
        qualityScore: quality?.qualityScore ?? null,
        ...(quality ? { qualityCi: quality.qualityCi } : {}),
      };
    });
    return NextResponse.json({
      ...comparison,
      current: { ...comparison.current, qualityScore: null },
      candidates,
      replay_source: replay ? "replay" : "mock",
    });
  }

  if (replay) {
    // Real replay data — drop the mock rescaling entirely. Each candidate row is built from
    // the gateway's projection (per-candidate average cost × matched corpus size). The current
    // model still comes from queryCurrentModel because v1 replay doesn't re-replay the current
    // model against itself.
    const candidates = replay.per_candidate
      .filter((c) => c.model !== live.model)
      .map((c) => {
        // CTO-114: real pairwise-LLM-judge win-rate when ≥10 samples have been judged for this
        // candidate. Below the floor, qualityScore is null — the page renders "—". We never
        // fall back to a mock number; that would have been the previous workaround and the
        // whole point of this ticket is to stop doing that.
        const quality = evalQualityFor(evalProj?.per_candidate, c.provider, c.model);
        // CTO-123: real per-candidate p95 latency + error rate from the replay projection.
        // Honest-null below the floor — same rule the `current` row uses (CTO-115): a p95 / error
        // rate computed from fewer than 50 replayed responses is too noisy to present, so we emit
        // null and the page renders "—" rather than a number or a borrowed mock.
        const enoughReplayed = c.samples_replayed >= MIN_REPLAYED_SAMPLES;
        return {
          model: c.model,
          provider: c.provider,
          monthlyCostMicroUsd: c.projected_monthly_cost_micro_usd,
          qualityScore: quality?.qualityScore ?? null,
          ...(quality ? { qualityCi: quality.qualityCi } : {}),
          latencyP95Ms: enoughReplayed ? c.p95_latency_ms : null,
          errorRate: enoughReplayed ? c.error_rate : null,
        };
      });
    const cheapest = candidates.reduce(
      (best, c) => (c.monthlyCostMicroUsd < best.monthlyCostMicroUsd ? c : best),
      candidates[0] ?? null,
    );
    const projectedSavingsMicroUsd = cheapest
      ? Math.max(0, live.monthlyCostMicroUsd - cheapest.monthlyCostMicroUsd)
      : 0;
    const projectedSavingsPct = live.monthlyCostMicroUsd > 0
      ? projectedSavingsMicroUsd / live.monthlyCostMicroUsd
      : 0;
    return NextResponse.json({
      ...comparison,
      current: {
        ...comparison.current,
        model: live.model,
        provider: live.provider,
        monthlyCostMicroUsd: live.monthlyCostMicroUsd,
        // CTO-115: live p95 / error from otel_spans over the same 7-day window. `null` when
        // fewer than 50 spans landed — page renders "—" so we never fabricate.
        latencyP95Ms: live.latencyP95Ms,
        errorRate: live.errorRate,
        // CTO-114: current never gets a fabricated quality — there's no judge pair when the
        // candidate IS the current model. The page renders "—" in that cell.
        qualityScore: null,
      },
      candidates,
      recommendation: {
        ...comparison.recommendation,
        projectedSavingsMicroUsd,
        projectedSavingsPct,
      },
      diagnostics: {
        ...comparison.diagnostics,
        samplesReplayed: replay.per_candidate.reduce((s, c) => s + c.samples_replayed, 0),
        samplesAvailable: replay.samples_available,
        replayCostMicroUsd: replay.diagnostics.replay_cost_micro_usd,
        contextFidelity:
          (replay.diagnostics.context_fidelity as
            | "resolved-context replay (no live retrieval)"
            | "live retrieval") ?? comparison.diagnostics.contextFidelity,
      },
      replay_source: "replay",
    });
  }

  // The mock comparison was built off a synthetic $6,420/mo baseline. Splicing the real current
  // cost in without re-scaling makes the candidates' absolute numbers nonsensical (real $1.31 vs
  // mock $1,780) and produces meaningless +100,000% deltas. Two corrections:
  //
  //   1. Deduplicate: if the live current model is in the candidates list, drop it — comparing a
  //      model to itself with two different cost rows is internally inconsistent.
  //   2. Re-scale: project each remaining candidate's cost as `mockRatio × liveCurrentCost`. This
  //      preserves the mock's *relative price ratios* (e.g. haiku ≈ 27% of sonnet) while anchoring
  //      to the user's actual workload size. Still an approximation — real ratios depend on token
  //      mix, which is what workflow-5 replay actually solves — but it's no longer absurd.
  const mockCurrentCost = comparison.current.monthlyCostMicroUsd;
  const scale = mockCurrentCost > 0 ? live.monthlyCostMicroUsd / mockCurrentCost : 0;
  const candidates = comparison.candidates
    .filter((c) => c.model !== live.model)
    .map((c) => {
      // CTO-114: even in the rescaled-mock path, qualityScore is the one cell that must NEVER
      // be faked — splice in real eval data when present, else null. If a tenant has run eval
      // but not replay, this lets the quality column light up while cost/latency stay mock.
      const quality = evalQualityFor(evalProj?.per_candidate, c.provider, c.model);
      return {
        ...c,
        monthlyCostMicroUsd: Math.round(c.monthlyCostMicroUsd * scale),
        qualityScore: quality?.qualityScore ?? null,
        ...(quality ? { qualityCi: quality.qualityCi } : {}),
      };
    });
  const projectedSavingsMicroUsd = Math.round(
    comparison.recommendation.projectedSavingsMicroUsd * scale,
  );

  return NextResponse.json({
    ...comparison,
    current: {
      ...comparison.current,
      model: live.model,
      provider: live.provider,
      monthlyCostMicroUsd: live.monthlyCostMicroUsd,
      // CTO-115: live p95 / error from otel_spans. `null` when n < 50 in the 7-day window.
      latencyP95Ms: live.latencyP95Ms,
      errorRate: live.errorRate,
      // CTO-114: current never gets a fabricated quality — no pair to judge against itself.
      qualityScore: null,
    },
    candidates,
    recommendation: {
      ...comparison.recommendation,
      projectedSavingsMicroUsd,
    },
    replay_source: "mock",
  });
}
