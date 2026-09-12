// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import {
  comparison,
  deriveRecommendation,
  deriveWorkload,
  scaleCandidateMonthlyCost,
} from "@/lib/compare";
import { sampleDataAllowed } from "@/lib/mock";
import {
  queryCurrentModel,
  queryEvalCandidates,
  queryReconcilerLastRun,
  queryReplayCandidates,
  type EvalCandidateRow,
  type ReplayProjection,
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

// CTO-168: the current-model cost window queryCurrentModel reads (last 7 days). Used to derive the
// `workload` label on the live path instead of shipping the fixture string.
const WORKLOAD_WINDOW_DAYS = 7;

// #320: the replay counts on every branch that has NOT run a replay. Null, not the fixture's
// 4,200 traces / 87,400 available / $42.30, which the page rendered as though they were measured.
// A count we did not take is unknown; the page's blank says so with the reason on hover.
const NO_REPLAY_DIAGNOSTICS = {
  samplesReplayed: null,
  samplesAvailable: null,
  replayCostMicroUsd: null,
} as const;

/**
 * The replay corpus counts a projection actually measured (#329).
 *
 * These describe the replay CORPUS, not the candidate table, which is why they are derived
 * separately from `replay_source`: a projection can exist on a branch that still ships fixture
 * candidate rows, and nulling its counts there put a measured number behind a blank whose reason
 * read "no cross-provider replay has run for this workload". A wrong reason on a blank is its own
 * honesty failure, so whenever the projection exists we report what it counted.
 */
function replayDiagnostics(replay: ReplayProjection | null) {
  if (!replay) return NO_REPLAY_DIAGNOSTICS;
  return {
    samplesReplayed: replay.per_candidate.reduce((s, c) => s + c.samples_replayed, 0),
    samplesAvailable: replay.samples_available,
    replayCostMicroUsd: replay.diagnostics.replay_cost_micro_usd,
  };
}

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

  // CTO-169: the baseline's freshness signal is the real reconciliation_runs last-run, not the
  // fixture constant — null (→ `—`) when the reconciler has never run / the source is unavailable.
  const reconcilerLastRunMinutesAgo = await queryReconcilerLastRun();

  // The "current model" half is the one we can ground in real traffic today: most-trafficked
  // model over the last 7 days. Quality/latency on `current` stay mocked because we don't yet
  // have an eval harness — flagged honestly via SyntheticPreviewBanner on the page.
  const live = await queryCurrentModel();
  if (!live && !sampleDataAllowed()) {
    // #364 finished here (CTO-379). Every other fixture fallback in app/api/** was put behind
    // sampleDataAllowed(); this route was missed, so it stayed the one surface that still answered
    // "this workspace has no traffic" with the storyline from lib/compare: a $19,100/mo incumbent,
    // a candidate table with costs and latencies, and a recommendation to switch. A customer who
    // signed in and sent nothing was shown a migration plan for models they have never called.
    //
    // There is nothing to fall back TO here: without an incumbent there is no baseline, and every
    // figure on the page is derived from one. So the honest answer is the empty one, and the page
    // reads it as the new-workspace case and points at setup.
    return NextResponse.json({
      ...comparison,
      workload: null,
      current: null,
      candidates: [],
      recommendation: null,
      diagnostics: {
        ...comparison.diagnostics,
        ...replayDiagnostics(replay),
        reconcilerLastRunMinutesAgo,
      },
      replay_source: "none",
    });
  }
  if (!live) {
    // Demo builds only, now that sampleDataAllowed() gates the branch above: a fixture incumbent
    // for screenshots. Even here, qualityScore must not be a fabricated number: splice real eval
    // data in if available, else null per-candidate.
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
      // #320: the fixture used to ship 4,200 / 87,400 / $42.30 here and the page printed them as
      // measurements. #329: null is only right when there is nothing to count. A projection can
      // come back with no incumbent behind it (a replay corpus is opted into per workload and does
      // not need a current-model cost row), and those counts are real, so they are reported.
      diagnostics: {
        ...comparison.diagnostics,
        ...replayDiagnostics(replay),
        reconcilerLastRunMinutesAgo,
      },
      // #329: "mock" describes the candidate rows above, which are the fixture's on this branch
      // whether or not a projection exists, because rescaling a candidate onto a monthly basis
      // needs the incumbent's call volume and there is no incumbent here. Calling this "replay"
      // told a consumer the cost table came from a replay it did not come from.
      replay_source: "mock",
    });
  }

  if (replay) {
    // Real replay data — drop the mock rescaling entirely. Each candidate row is built from
    // the gateway's projection. The current model still comes from queryCurrentModel because v1
    // replay doesn't re-replay the current model against itself.
    const candidates = replay.per_candidate
      .filter((c) => c.model !== live.model)
      .flatMap((c) => {
        // CTO-231: the gateway's projected_monthly_cost_micro_usd is the candidate's cost over the
        // REPLAYED CORPUS (per-candidate average cost × matched corpus size), NOT a full month of
        // traffic. Shipping it as-is against the incumbent's real full-month spend made the
        // candidate look ~$8/mo next to a ~$72K/mo current model, so deriveRecommendation reported
        // a nonsense "100% reduction". Rescale it onto the SAME full-traffic monthly basis as
        // `current`: per-call cost (corpus cost / samples replayed) × the current model's
        // full-traffic monthly call count. A candidate with no replayed responses has no per-call
        // cost, so scaleCandidateMonthlyCost returns null and we emit nothing for it (honest-null)
        // rather than divide by zero; its absence surfaces as the insufficient-data verdict below.
        const monthlyCostMicroUsd = scaleCandidateMonthlyCost(
          c.projected_monthly_cost_micro_usd,
          c.samples_replayed,
          live.monthlyCalls,
        );
        if (monthlyCostMicroUsd === null) return [];
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
        return [
          {
            model: c.model,
            provider: c.provider,
            monthlyCostMicroUsd,
            qualityScore: quality?.qualityScore ?? null,
            ...(quality ? { qualityCi: quality.qualityCi } : {}),
            latencyP95Ms: enoughReplayed ? c.p95_latency_ms : null,
            errorRate: enoughReplayed ? c.error_rate : null,
          },
        ];
      });
    // CTO-168: verdict + summary + projected savings are all generated from the REAL replayed
    // deltas here — cost savings %, pairwise-judge quality, latency — never the fixture prose.
    // Gated on total replayed responses; a thin projection yields an honest "insufficient data"
    // summary rather than a confident recommendation off noise.
    const totalReplayed = replay.per_candidate.reduce((s, c) => s + c.samples_replayed, 0);
    const recommendation = deriveRecommendation({
      currentModel: live.model,
      currentCostMicroUsd: live.monthlyCostMicroUsd,
      candidates: candidates.map((c) => ({
        model: c.model,
        monthlyCostMicroUsd: c.monthlyCostMicroUsd,
        qualityScore: c.qualityScore,
        latencyP95Ms: c.latencyP95Ms,
      })),
      samplesReplayed: totalReplayed,
    });
    return NextResponse.json({
      ...comparison,
      // CTO-168: real query context (tag filter + 7-day window), not the fixture label.
      workload: deriveWorkload(featureTag, WORKLOAD_WINDOW_DAYS),
      current: {
        ...comparison.current,
        model: live.model,
        provider: live.provider,
        // CTO-244 follow-up: an unknown incumbent cost now travels as null, not as the 0 that
        // used to stand in as the "no baseline" sentinel. The page's own gate reads null as no
        // baseline (same banner) AND the tile renders the explained blank instead of "$0.00/mo",
        // which is what the sentinel cost us: a fabricated zero everywhere the gate did not reach.
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
      recommendation,
      diagnostics: {
        ...comparison.diagnostics,
        samplesReplayed: totalReplayed,
        samplesAvailable: replay.samples_available,
        replayCostMicroUsd: replay.diagnostics.replay_cost_micro_usd,
        contextFidelity:
          (replay.diagnostics.context_fidelity as
            | "resolved-context replay (no live retrieval)"
            | "live retrieval") ?? comparison.diagnostics.contextFidelity,
        reconcilerLastRunMinutesAgo,
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
  const mockCurrentCost = comparison.current.monthlyCostMicroUsd ?? 0;
  // CTO-244 follow-up: with an unknown live cost there is nothing to anchor the rescale to, so
  // there is no candidate figure either. It used to fall back to scale 0, which collapsed every
  // candidate row to "$0.00" (and, divided against the unknown incumbent, printed a literal
  // "NaN%" beside it). A null scale means null candidate costs: honest blanks, not free models.
  const scale =
    mockCurrentCost > 0 && live.monthlyCostMicroUsd !== null
      ? live.monthlyCostMicroUsd / mockCurrentCost
      : null;
  const candidates = comparison.candidates
    .filter((c) => c.model !== live.model)
    .map((c) => {
      // CTO-114: even in the rescaled-mock path, qualityScore is the one cell that must NEVER
      // be faked — splice in real eval data when present, else null. If a tenant has run eval
      // but not replay, this lets the quality column light up while cost/latency stay mock.
      const quality = evalQualityFor(evalProj?.per_candidate, c.provider, c.model);
      return {
        ...c,
        monthlyCostMicroUsd:
          scale === null || c.monthlyCostMicroUsd === null
            ? null
            : Math.round(c.monthlyCostMicroUsd * scale),
        qualityScore: quality?.qualityScore ?? null,
        ...(quality ? { qualityCi: quality.qualityCi } : {}),
      };
    });
  // CTO-168: this branch has a LIVE current model but only rescaled-mock candidate costs — there
  // is no real cross-provider replay behind it. So we do NOT ship the fixture verdict/summary
  // (that would be the hardcoded "$12.2K/mo … haiku-4.5" prose on live data). Instead we pass
  // samplesReplayed: 0, which deriveRecommendation surfaces as an honest "insufficient replay data"
  // recommendation while still projecting savings off the rescaled cheapest candidate.
  const recommendation = deriveRecommendation({
    currentModel: live.model,
    currentCostMicroUsd: live.monthlyCostMicroUsd,
    // A candidate with no projected cost cannot be the cheapest one, so it is not a candidate for
    // the recommendation at all (CTO-244 follow-up).
    candidates: candidates.flatMap((c) =>
      c.monthlyCostMicroUsd === null
        ? []
        : [
            {
              model: c.model,
              monthlyCostMicroUsd: c.monthlyCostMicroUsd,
              qualityScore: c.qualityScore,
              // Candidate latency here is still the fixture mock (no replay); deriveRecommendation
              // ignores it on the insufficient-data path, so no mock latency leaks into the summary.
              latencyP95Ms: c.latencyP95Ms,
            },
          ],
    ),
    samplesReplayed: 0,
  });

  return NextResponse.json({
    ...comparison,
    // CTO-168: real query context (tag filter + 7-day window), not the fixture label.
    workload: deriveWorkload(featureTag, WORKLOAD_WINDOW_DAYS),
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
    recommendation,
    // #320: this branch has a live current model and rescaled-mock candidates, and no replay at
    // all. It is the branch the issue was reported against: nine spans in the stack, "$42.30 /
    // 4,200 traces replayed / 87,400 prod traces" on screen. All four counts are null now.
    diagnostics: { ...comparison.diagnostics, ...NO_REPLAY_DIAGNOSTICS, reconcilerLastRunMinutesAgo },
    replay_source: "mock",
  });
}
