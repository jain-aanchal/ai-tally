// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from "next/server";

import { queryConnectorActivity } from "@/lib/clickhouse";
import { CONNECTORS, applyActivity, mockActivity } from "@/lib/connectors";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const activity = await queryConnectorActivity();
  const live = activity !== null;
  const connectors = applyActivity(CONNECTORS, activity ?? mockActivity);
  return NextResponse.json({ connectors, live });
}
