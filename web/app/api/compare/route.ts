// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";
import { comparison } from "@/lib/compare";
import { queryCurrentModel } from "@/lib/clickhouse";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  // The candidates / quality / latency / recommendation halves remain mock until workflow-5
  // replay infra lands. The "current model" half is the one we can ground in real traffic today:
  // ServiceName/model with the most spans over the last 7 days. Quality/latency on `current` stay
  // mocked because we don't yet have an eval harness — flagged honestly via SyntheticPreviewBanner
  // on the page when there's no real data to back the projection.
  const live = await queryCurrentModel();
  if (!live) {
    return NextResponse.json(comparison);
  }
  return NextResponse.json({
    ...comparison,
    current: {
      ...comparison.current,
      model: live.model,
      provider: live.provider,
      monthlyCostMicroUsd: live.monthlyCostMicroUsd,
    },
  });
}
