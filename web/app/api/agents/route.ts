// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { type AgentRun, type AgentSummary, agents, runs } from "@/lib/agents";
import { queryAgents, queryReconcilerLastRun } from "@/lib/clickhouse";
import { type SourceState, readState } from "@/lib/dataState";
import { parseFilters, rangeDays } from "@/lib/filters";
import { sampleDataAllowed } from "@/lib/mock";

// Read live data per request (never statically cached). A read that fails says so; it is not
// answered with fixture numbers (#364).
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export interface AgentsResponse {
  agents: AgentSummary[];
  runs: AgentRun[];
  reconcilerLastRunMinutesAgo: number | null;
  sources: { agents: SourceState; runs: SourceState };
}

export async function GET(req: Request) {
  // Optional URL filters (CTO-104): /api/agents?tag=aider-demo&run=<trace>. Empty values pass
  // through and the SQL clause is dropped.
  // Use the standard URL API rather than NextRequest.nextUrl so unit tests can pass plain Request.
  const { searchParams } = new URL(req.url);
  const tag = searchParams.get("tag") ?? "";
  const run = searchParams.get("run") ?? "";
  // ?agent=<ServiceName> (CTO-241): the unified Cost explorer requests one agent's detail when
  // group-by=agent is narrowed to a single agent.
  const agent = searchParams.get("agent") ?? "";
  // The time-range selector drives the windowed cost/day average (CTO-226): resolve the URL-synced
  // filter state to a day count the ClickHouse-derived window clamps and interpolates.
  const windowDays = rangeDays(parseFilters(searchParams).range);
  // Read agents telemetry and the reconciler's real last-run in parallel. The freshness signal is
  // the real reconciliation_runs value (CTO-169), or null when the reconciler has never run / the
  // gateway is unavailable, which the page renders as a blank rather than a fabricated constant.
  const [live, reconcilerLastRunMinutesAgo] = await Promise.all([
    queryAgents({ tag, run, agent }, windowDays),
    queryReconcilerLastRun(),
  ]);

  // #364: the fixture roster is a demo build's, and only a demo build's. It used to fill in for
  // BOTH an unreachable ClickHouse and a tenant with no agents yet, which meant a new customer's
  // Cost explorer listed agents they have never run. The old `hasFilter` guard only ever covered
  // the filtered views; the unfiltered one, which is the one every new tenant lands on, was the
  // hole. Fixtures are never filter-scoped either, so they stay out of a filtered view regardless.
  if (sampleDataAllowed() && !tag && !run && !agent) {
    return NextResponse.json({
      agents,
      runs,
      reconcilerLastRunMinutesAgo,
      sources: { agents: "sample", runs: "sample" },
    } satisfies AgentsResponse);
  }

  return NextResponse.json({
    agents: live?.agents ?? [],
    runs: live?.runs ?? [],
    reconcilerLastRunMinutesAgo,
    sources: {
      agents: readState(live, (l) => l.agents.length === 0),
      runs: readState(live, (l) => l.runs.length === 0),
    },
  } satisfies AgentsResponse);
}
