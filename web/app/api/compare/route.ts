// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { comparison } from "@/lib/compare";
import { queryCurrentModel, queryReplayCandidates } from "@/lib/clickhouse";

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

  // The "current model" half is the one we can ground in real traffic today: most-trafficked
  // model over the last 7 days. Quality/latency on `current` stay mocked because we don't yet
  // have an eval harness — flagged honestly via SyntheticPreviewBanner on the page.
  const live = await queryCurrentModel();
  if (!live) {
    return NextResponse.json({
      ...comparison,
      diagnostics: {
        ...comparison.diagnostics,
        // Tag the response so the page can render the right banner copy.
        ...(replay ? {} : {}),
      },
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
      .map((c) => ({
        model: c.model,
        provider: c.provider,
        monthlyCostMicroUsd: c.projected_monthly_cost_micro_usd,
        // Quality score still mock until an LLM-judge eval lands — surface honestly.
        qualityScore:
          comparison.candidates.find((m) => m.model === c.model)?.qualityScore ?? 0.9,
        latencyP95Ms: c.p95_latency_ms || comparison.candidates[0]?.latencyP95Ms || 0,
        errorRate: c.error_rate,
      }));
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
    .map((c) => ({
      ...c,
      monthlyCostMicroUsd: Math.round(c.monthlyCostMicroUsd * scale),
    }));
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
    },
    candidates,
    recommendation: {
      ...comparison.recommendation,
      projectedSavingsMicroUsd,
    },
    replay_source: "mock",
  });
}
