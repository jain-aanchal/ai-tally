// SPDX-License-Identifier: Apache-2.0
// The waste-detection aggregation endpoint (CTO-234, W7 of epic CTO-227). This is the single surface
// the waste page reads: it runs every detector over the SAME URL-synced filter state the FilterBar
// writes (range / from / to / feature / model / provider / layer / account) and rolls the findings
// up with the pure `aggregateWaste`, so a shared dashboard link and its data request are one string.
//
// Why run all five concurrently and tolerate empties (CTO-227 boundary): each detector already
// tenant-scopes, windows off the ClickHouse clock, and returns `[]` when its data is unavailable, so
// one dead detector must never blank the whole page. Promise.all fans them out; the flat union feeds
// the roll-up.
//
// Honesty posture (CLAUDE.md, CTO-227): this endpoint never fabricates a finding. The detectors emit
// nothing on unavailability rather than a guessed figure, and `aggregateWaste` keeps recoverable
// dollars null (an honest blank) rather than 0 when they cannot be bounded. Only a HARD failure (a
// thrown error) sets `report.unavailable` to a real reason on an otherwise-empty report shell.

import { NextResponse } from "next/server";

import { collectPaidForNothing } from "@/lib/waste/paid-for-nothing";
import { collectDuplicatedWork } from "@/lib/waste/duplicated-work";
import { collectWrongSizedModel } from "@/lib/waste/wrong-sized-model";
import { collectNoMeasuredReturn } from "@/lib/waste/no-measured-return";
import { collectStructuralInefficiency } from "@/lib/waste/structural-inefficiency";
import { aggregateWaste, type WasteReport } from "@/lib/waste";
import { parseFilters, rangeDays } from "@/lib/filters";

// Read live data per request (never statically cached): the window and dimension filters come off the
// URL, so the report is dynamic, not static.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request) {
  // Plain URL API (not NextRequest.nextUrl) so unit tests can pass a bare Request.
  const sp = new URL(request.url).searchParams;
  const state = parseFilters(sp);
  // Window is the URL time-range resolved to a day count; the detectors clamp it against the
  // ClickHouse clock. Never hardcoded (CTO-234).
  const windowDays = rangeDays(state.range);

  try {
    // Fan every detector out over the same (windowDays, filters). Each returns [] on unavailability,
    // so a single dead detector never blanks the page (CTO-227).
    const perDetector = await Promise.all([
      collectPaidForNothing(windowDays, state.filters),
      collectDuplicatedWork(windowDays, state.filters),
      collectWrongSizedModel(windowDays, state.filters),
      collectNoMeasuredReturn(windowDays, state.filters),
      collectStructuralInefficiency(windowDays, state.filters),
    ]);
    const allFindings = perDetector.flat();
    return NextResponse.json(aggregateWaste(allFindings, windowDays));
  } catch (err) {
    // Hard failure: return an honest unavailable shell (empty findings, null totals) rather than a
    // fabricated or partial report (CTO-227 honesty). Never invent findings.
    const reason = err instanceof Error ? err.message : "waste report unavailable";
    const shell: WasteReport = {
      findings: [],
      totalRecoverableMicroUsd: null,
      byCategory: {
        paid_for_nothing: null,
        duplicated_work: null,
        wrong_sized_model: null,
        no_measured_return: null,
        structural_inefficiency: null,
      },
      generatedForWindowDays: windowDays,
      unavailable: reason,
    };
    return NextResponse.json(shell);
  }
}
