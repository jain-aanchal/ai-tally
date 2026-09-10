// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  type IntegrationStatusRow,
  queryConnectorActivity,
  queryIntegrationStatus,
} from "@/lib/clickhouse";
import { CONNECTORS, applyActivity, mockActivity } from "@/lib/connectors";
import { type SourceState, readState } from "@/lib/dataState";
import { sampleDataAllowed } from "@/lib/mock";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** No observed activity at all: every catalog row renders as "Not connected", which is the truth. */
const NO_ACTIVITY = { records: {}, lastAt: {} } as const;

export async function GET() {
  const [activity, integrations] = await Promise.all([
    queryConnectorActivity(),
    queryIntegrationStatus(),
  ]);
  // CTO-117: prefer real per-tenant third-party integration status from the gateway. When the
  // gateway is unreachable OR a tenant has no rows yet, we still emit the catalog cards and let
  // the UI render them as "Not connected" (no rows → no fabricated stats).
  const integrationsLive = integrations !== null && integrations.length > 0;

  // #364: `activity ?? mockActivity` put 4,120 LLM-proxy records and 86 Stripe records with May
  // timestamps on a tenant that has connected nothing, and did it on the page whose entire job is
  // to tell you what you have connected. A source we cannot read is reported as unreadable; a
  // source with no records is reported as not connected. Neither is a reason to invent records.
  const sample = sampleDataAllowed();
  const state: SourceState = sample
    ? "sample"
    : readState(activity, (a) => Object.keys(a.records).length === 0);
  const connectors = applyActivity(
    CONNECTORS,
    sample ? mockActivity : (activity ?? NO_ACTIVITY),
  );
  return NextResponse.json({
    connectors,
    // `live` is kept for the existing consumers and now means exactly what it says: real observed
    // activity was read. `activity` carries the state those consumers need to tell empty from down.
    live: state === "live",
    activity: state,
    integrations: integrations ?? [],
    integrationsLive,
    integrationsState: readState(integrations, (i) => i.length === 0),
  } satisfies {
    connectors: ReturnType<typeof applyActivity>;
    live: boolean;
    activity: SourceState;
    integrations: IntegrationStatusRow[];
    integrationsLive: boolean;
    integrationsState: SourceState;
  });
}
