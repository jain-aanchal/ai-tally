// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { EMPTY_PROJECTION, projection, type WhatIfProjection } from "@/lib/estimate";
import { sampleDataAllowed } from "@/lib/mock";
import {
  queryReconcilerLastRun,
  queryReplayCandidates,
  queryReplayEstimate,
} from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Honest-null floor: a what-if grounded on too few replayed samples is noise, not a forecast.
// Below this count the route returns null cost/latency so the page renders a blank instead of a
// fabricated number (CTO-128).
const MIN_GROUNDING_SAMPLES = 50;

/**
 * The fixture is the answer only where sampleDataAllowed() permits it (CTO-298).
 *
 * This route was the last one in app/api/** returning a fixture unconditionally. #364 put every
 * other fallback behind this gate and /api/compare followed in CTO-379; /estimate was missed
 * because it is unlinked from the nav, which protects nobody: middleware.ts treats it as an
 * ordinary signed-in route, so a pilot user typing the URL or following an old link reached a
 * $19,100/mo baseline, a 42% blow-up risk and a pull request (#1284) that does not exist in their
 * repo, with no banner, presented as their own workload.
 *
 * There is nothing to fall back TO for a real tenant: an estimate is a projection off a measured
 * baseline, and without a replayed corpus there is no baseline and no driver breakdown. So the
 * honest answer is the empty one, and the page reads it as the new-workspace case.
 */
function fallbackProjection(reconcilerLastRunMinutesAgo: number | null) {
  const base = sampleDataAllowed() ? projection : EMPTY_PROJECTION;
  return NextResponse.json({
    ...base,
    reconcilerLastRunMinutesAgo,
    replay_source: base.synthetic ? "mock" : "none",
  });
}

// CTO-113: /estimate accepts a what-if candidate via `?candidate_model=...&candidate_provider=...`
// and replays the captured corpus against it. When no candidate is supplied or no replay
// samples exist yet, the route answers with {@link fallbackProjection}. This is a
// minimal wiring on top of the existing GET surface; the richer body-driven what-if
// (prompt_template_override, sample_size override) is FIXME(CTO-113-estimate) below.
export async function GET(req: Request) {
  const url = new URL(req.url);
  const candidateModel = url.searchParams.get("candidate_model");
  const candidateProvider = url.searchParams.get("candidate_provider") ?? "anthropic";
  const featureTag = url.searchParams.get("tag") ?? undefined;

  // CTO-169: baseline freshness is the real reconciliation_runs last-run, not the fixture constant,
  // null (rendered as a blank) when the reconciler has never run / the source is unavailable.
  const reconcilerLastRunMinutesAgo = await queryReconcilerLastRun();

  if (!candidateModel) {
    return fallbackProjection(reconcilerLastRunMinutesAgo);
  }

  const replay = await queryReplayCandidates(featureTag, [
    { provider: candidateProvider, model: candidateModel },
  ]);
  if (!replay || replay.per_candidate.length === 0) {
    return fallbackProjection(reconcilerLastRunMinutesAgo);
  }

  // GET replays the captured envelope as-is against the candidate model (no prompt rewrite).
  // The richer body-driven what-if (candidate model + system_prompt_override) lives in POST below.
  //
  // CTO-298: built on EMPTY_PROJECTION, never on the fixture. This branch used to spread
  // `...projection`, so a genuine replay result still carried the invented PR, the 42% risk, the
  // three drivers and the $19,100 baseline, and only the one number the replay produced was real.
  const proposed = replay.per_candidate[0];
  return NextResponse.json({
    ...EMPTY_PROJECTION,
    reconcilerLastRunMinutesAgo,
    proposed: {
      monthlyCostMicroUsd: proposed.projected_monthly_cost_micro_usd,
      // Both null by CTO-298: see the Figures doc comment. p99 was monthly * 1.4 (a fixture
      // multiplier, not a percentile) and the mean was the replay row's p50.
      p99CostMicroUsd: null,
      meanLatencyMs: null,
    },
    sample: { ...EMPTY_PROJECTION.sample, used: proposed.samples_replayed },
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
  const [replay, reconcilerLastRunMinutesAgo] = await Promise.all([
    queryReplayEstimate({
      candidateModel: candidate,
      systemPromptOverride,
      featureTag,
      sampleSize,
    }),
    // CTO-169: real reconciler last-run (or null, rendered as a blank), not the fixture constant.
    queryReconcilerLastRun(),
  ]);

  const row = replay?.per_candidate[0];
  const grounded = row?.samples_replayed ?? 0;
  // Honest-null floor: too few samples grounding the estimate -> null cost, page renders a blank.
  const sufficient = !!row && grounded >= MIN_GROUNDING_SAMPLES;

  // CTO-298: the surrounding context (baseline, PR, drivers, blow-up risk) is the fixture's only on
  // the sample path. A real tenant's what-if carries the replayed figures and nothing else.
  const base = sampleDataAllowed() ? projection : EMPTY_PROJECTION;
  const result: WhatIfProjection = {
    ...base,
    reconcilerLastRunMinutesAgo,
    proposed: {
      monthlyCostMicroUsd: sufficient ? row!.projected_monthly_cost_micro_usd : null,
      // Null on every branch (CTO-298): per-call replay returns neither a cost distribution nor a
      // latency distribution, and deriving one from a monthly total or a p50 invents the number.
      p99CostMicroUsd: null,
      meanLatencyMs: null,
    },
    sample: {
      ...base.sample,
      used: grounded,
    },
    candidate,
    systemPromptOverride,
    groundedSamples: grounded,
    replay_source: sufficient ? "replay" : base.synthetic ? "mock" : "none",
  };

  return NextResponse.json(result);
}
