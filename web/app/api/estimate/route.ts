// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { projection } from "@/lib/estimate";
import { queryReplayCandidates } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// CTO-113: /estimate accepts a what-if candidate via `?candidate_model=...&candidate_provider=...`
// and replays the captured corpus against it. When no candidate is supplied or no replay
// samples exist yet, the route returns the original mock projection unchanged. This is a
// minimal wiring on top of the existing GET surface; the richer body-driven what-if
// (prompt_template_override, sample_size override) is FIXME(CTO-113-estimate) below.
export async function GET(req: Request) {
  const url = new URL(req.url);
  const candidateModel = url.searchParams.get("candidate_model");
  const candidateProvider = url.searchParams.get("candidate_provider") ?? "anthropic";
  const featureTag = url.searchParams.get("tag") ?? undefined;

  if (!candidateModel) {
    return NextResponse.json({ ...projection, replay_source: "mock" });
  }

  const replay = await queryReplayCandidates(featureTag, [
    { provider: candidateProvider, model: candidateModel },
  ]);
  if (!replay || replay.per_candidate.length === 0) {
    return NextResponse.json({ ...projection, replay_source: "mock" });
  }

  // FIXME(CTO-113-estimate): prompt_template_override is not yet wired through. v1 only
  // replays the captured envelope as-is against the candidate model — no prompt rewrite.
  // The richer body-driven what-if surface (POST {candidate_model, prompt_template_override,
  // sample_size}) is a follow-up.

  const proposed = replay.per_candidate[0];
  return NextResponse.json({
    ...projection,
    proposed: {
      monthlyCostMicroUsd: proposed.projected_monthly_cost_micro_usd,
      // p99 cost not available from per-call replay yet — keep the mock p99 multiplier as a
      // rough proxy until the executor returns the full distribution.
      p99CostMicroUsd: Math.round(proposed.projected_monthly_cost_micro_usd * 1.4),
      meanLatencyMs: proposed.p50_latency_ms,
    },
    sample: {
      ...projection.sample,
      used: replay.per_candidate[0].samples_replayed,
    },
    replay_source: "replay",
  });
}
