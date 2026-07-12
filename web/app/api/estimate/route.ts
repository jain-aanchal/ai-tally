// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { projection, type WhatIfProjection } from "@/lib/estimate";
import { queryReplayCandidates, queryReplayEstimate } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Honest-null floor: a what-if grounded on too few replayed samples is noise, not a forecast.
// Below this count the route returns null cost/latency so the page renders "—" instead of a
// fabricated number (CTO-128).
const MIN_GROUNDING_SAMPLES = 50;

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

  // GET replays the captured envelope as-is against the candidate model (no prompt rewrite).
  // The richer body-driven what-if (candidate model + system_prompt_override) lives in POST below.
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

// Body-driven what-if (CTO-128): swap a candidate model and optionally tighten the system prompt,
// then re-project cost off the captured corpus. Returns the Projection shape the page consumes,
// with the honest-null floor applied to the proposed numbers.
//
// Body: { candidateModel: string, providerOverride?: string, systemPromptOverride?: string,
//         sampleSize?: number, tag?: string }
export async function POST(req: Request) {
  let body: {
    candidateModel?: string;
    providerOverride?: string;
    systemPromptOverride?: string;
    sampleSize?: number;
    tag?: string;
  };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const candidateModel = (body.candidateModel ?? "").trim();
  if (!candidateModel) {
    return NextResponse.json({ error: "candidateModel is required" }, { status: 400 });
  }
  const provider = (body.providerOverride ?? "anthropic").trim() || "anthropic";
  const systemPromptOverride = body.systemPromptOverride?.trim() || undefined;
  const featureTag = body.tag?.trim() || undefined;
  const sampleSize =
    typeof body.sampleSize === "number" && body.sampleSize > 0
      ? Math.floor(body.sampleSize)
      : undefined;

  const candidate = { provider, model: candidateModel };
  const replay = await queryReplayEstimate({
    candidateModel: candidate,
    systemPromptOverride,
    featureTag,
    sampleSize,
  });

  const row = replay?.per_candidate[0];
  const grounded = row?.samples_replayed ?? 0;
  // Honest-null floor: too few samples grounding the estimate -> null cost, page renders "—".
  const sufficient = !!row && grounded >= MIN_GROUNDING_SAMPLES;

  const monthly = sufficient ? row!.projected_monthly_cost_micro_usd : null;
  const result: WhatIfProjection = {
    ...projection,
    proposed: {
      monthlyCostMicroUsd: monthly,
      // p99 cost not available from per-call replay yet — keep the mock 1.4x multiplier as a
      // rough proxy until the executor returns the full distribution.
      p99CostMicroUsd: monthly === null ? null : Math.round(monthly * 1.4),
      meanLatencyMs: sufficient ? row!.p50_latency_ms : null,
    },
    sample: {
      ...projection.sample,
      used: grounded,
    },
    candidate,
    systemPromptOverride,
    groundedSamples: grounded,
    replay_source: sufficient ? "replay" : "mock",
  };

  return NextResponse.json(result);
}
