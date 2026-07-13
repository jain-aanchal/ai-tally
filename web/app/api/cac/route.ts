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
  if (live.periods.length > 0) {
    // Real gateway data. Economics (ARPA + gross margin) come from the same cac_periods rows
    // (CTO-145, Option A); months where finance hasn't entered both are simply absent from the
    // map, so the page renders payback / LTV as "—" (honest-null) for those.
    return NextResponse.json({
      periods: live.periods,
      economics: live.economics,
      isMock: false,
    });
  }
  return NextResponse.json({
    periods: MOCK_CAC_PERIODS,
    economics: MOCK_PERIOD_ECONOMICS,
    isMock: true,
  });
}
