// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { dq } from "@/lib/dq";
import { queryDataQualityReport } from "@/lib/clickhouse";

// Read live data per request (never statically cached); fall back to mock when ClickHouse is down.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const live = await queryDataQualityReport();
  return NextResponse.json(live ?? dq);
}
