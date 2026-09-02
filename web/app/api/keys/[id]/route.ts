// SPDX-License-Identifier: Apache-2.0
// Revoke one ingest key (Initiative 1, §5). Admin-only. Forwards DELETE to the gateway, which sets
// revoked_at = now() (a real revoke, not a delete). Returns the gateway's 204.
import { NextResponse } from "next/server";

import { canManage, controlPlaneHeaders, getTenant } from "@/lib/getTenant";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export async function DELETE(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await params;
  const tenant = await getTenant();
  if (!canManage(tenant)) {
    return NextResponse.json({ error: "admin role required to revoke keys" }, { status: 403 });
  }
  const res = await fetch(`${GATEWAY_URL}/v1/tenant/keys/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: controlPlaneHeaders(tenant.tenantId),
  });
  return new NextResponse(null, { status: res.status });
}
