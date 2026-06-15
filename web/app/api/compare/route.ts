// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { comparison } from "@/lib/compare";
import { queryCurrentModel } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  // The candidates / quality / latency / recommendation halves remain mock until workflow-5
  // replay infra lands. The "current model" half is the one we can ground in real traffic today:
  // most-trafficked model over the last 7 days. Quality/latency on `current` stay mocked because
  // we don't yet have an eval harness — flagged honestly via SyntheticPreviewBanner on the page.
  const live = await queryCurrentModel();
  if (!live) {
    return NextResponse.json(comparison);
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
    },
    candidates,
    recommendation: {
      ...comparison.recommendation,
      projectedSavingsMicroUsd,
    },
  });
}
