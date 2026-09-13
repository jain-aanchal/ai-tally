// SPDX-License-Identifier: Apache-2.0
// Server-side seam for the hosted-proxy switch. Like /api/keys, the browser never holds the gateway
// service token, and this handler is where the admin role is enforced: the gateway sees only the
// service token and the resolved tenant, so it cannot tell an admin from a member.
import { NextResponse } from "next/server";

import { canManage, controlPlaneHeaders, currentUserId, getTenant } from "@/lib/getTenant";
import { queryProxyEnabled } from "@/lib/proxySetting";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** Any member may read it; the Connect snippets need to know whether they will work. */
export async function GET(): Promise<NextResponse> {
  const tenant = await getTenant();
  return NextResponse.json({ enabled: await queryProxyEnabled(tenant.tenantId) });
}

/** Admins only. Body: `{ enabled: boolean }`. */
export async function POST(req: Request): Promise<NextResponse> {
  const tenant = await getTenant();
  if (!canManage(tenant)) {
    return NextResponse.json(
      { error: "admin role required to change the hosted proxy setting" },
      { status: 403 },
    );
  }
  const input = (await req.json().catch(() => null)) as { enabled?: unknown } | null;
  // Checked here as well as at the gateway so a malformed request never leaves the server.
  if (!input || typeof input.enabled !== "boolean") {
    return NextResponse.json({ error: "enabled must be a boolean" }, { status: 400 });
  }
  const userId = await currentUserId();
  const res = await fetch(`${GATEWAY_URL}/v1/tenant/proxy/config`, {
    method: "POST",
    headers: controlPlaneHeaders(tenant.tenantId, {
      "content-type": "application/json",
      ...(userId ? { "x-clerk-user-id": userId } : {}),
    }),
    body: JSON.stringify({ enabled: input.enabled }),
    cache: "no-store",
  });
  const body = (await res.json().catch(() => ({}))) as {
    config?: { enabled?: boolean };
    detail?: unknown;
  };
  if (!res.ok) {
    return NextResponse.json(
      { error: `could not save the setting (gateway HTTP ${res.status})` },
      { status: res.status },
    );
  }
  return NextResponse.json({ enabled: body.config?.enabled ?? input.enabled });
}
