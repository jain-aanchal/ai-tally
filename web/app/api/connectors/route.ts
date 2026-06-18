// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import {
  type IntegrationStatusRow,
  queryConnectorActivity,
  queryIntegrationStatus,
} from "@/lib/clickhouse";
import { CONNECTORS, applyActivity, mockActivity } from "@/lib/connectors";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const [activity, integrations] = await Promise.all([
    queryConnectorActivity(),
    queryIntegrationStatus(),
  ]);
  const live = activity !== null;
  // CTO-117: prefer real per-tenant third-party integration status from the gateway. When the
  // gateway is unreachable OR a tenant has no rows yet, we still emit the catalog cards and let
  // the UI render them as "Not connected" (no rows → no fabricated stats). The legacy
  // mockActivity remains the cost/revenue source-of-activity fallback below.
  const integrationsLive = integrations !== null && integrations.length > 0;
  const baseActivity = activity ?? mockActivity;
  const connectors = applyActivity(CONNECTORS, baseActivity);
  return NextResponse.json({
    connectors,
    live,
    integrations: integrations ?? [],
    integrationsLive,
  } satisfies {
    connectors: ReturnType<typeof applyActivity>;
    live: boolean;
    integrations: IntegrationStatusRow[];
    integrationsLive: boolean;
  });
}
