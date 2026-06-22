// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  MOCK_CAC_PERIODS,
  MOCK_PERIOD_ECONOMICS,
  queryCacPeriods,
  type CacPeriod,
  type PeriodEconomics,
} from "@/lib/cac";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface CacPayload {
  periods: CacPeriod[];
  /** Per-period revenue-side economics, keyed by `periodStart`. Absent ⇒ payback/LTV render "—". */
  economics: Record<string, PeriodEconomics>;
  /** True when the gateway was unreachable / empty and we fell back to the labelled mock. */
  isMock: boolean;
}

export async function GET(): Promise<NextResponse<CacPayload>> {
  // queryCacPeriods returns [] when the gateway is unreachable OR has no rows (CI / fresh clone).
  // Mirror the established fallback (see /api/attribution, /api/cost): serve the clearly-labelled
  // mock so the page is useful before the CAC backend has data, never fabricate as real.
  const live = await queryCacPeriods();
  if (live.length > 0) {
    // Real gateway data: no economics yet (the CAC backend doesn't store ARPA / gross margin), so
    // payback / LTV honest-null until a revenue source is wired. Periods + CAC flavors are real.
    return NextResponse.json({ periods: live, economics: {}, isMock: false });
  }
  return NextResponse.json({
    periods: MOCK_CAC_PERIODS,
    economics: MOCK_PERIOD_ECONOMICS,
    isMock: true,
  });
}
