// SPDX-License-Identifier: Apache-2.0
// Ingest-key management proxy (Initiative 1, §5/§7). The browser must never hold the gateway service
// token, so these server-side handlers are the seam: they resolve the active tenant from Clerk,
// enforce the admin role for writes (§9), and forward to the gateway's service-token-authed keys
// endpoints. The raw token the gateway returns on create is passed straight back to the caller ONCE
// and never stored here.
import { NextResponse } from "next/server";

import { controlPlaneHeaders, currentUserId, getTenant, requireAdmin } from "@/lib/getTenant";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export async function GET(): Promise<NextResponse> {
  const tenant = await getTenant();
  const res = await fetch(`${GATEWAY_URL}/v1/tenant/keys`, {
    headers: controlPlaneHeaders(tenant.tenantId),
    cache: "no-store",
  });
  const body = await res.json().catch(() => ({}));
  return NextResponse.json(body, { status: res.status });
}

export async function POST(req: Request): Promise<NextResponse> {
  // CTO-392: the same gate as before, now the shared one every mutation goes through.
  const gate = await requireAdmin("mint keys");
  if (!gate.ok) return gate.response;
  const input = await req.json().catch(() => ({}));
  const userId = await currentUserId();
  const res = await fetch(`${GATEWAY_URL}/v1/tenant/keys`, {
    method: "POST",
    headers: controlPlaneHeaders(gate.tenant.tenantId, {
      "content-type": "application/json",
      ...(userId ? { "x-clerk-user-id": userId } : {}),
    }),
    body: JSON.stringify({ name: input?.name, scope: input?.scope }),
  });
  const body = await res.json().catch(() => ({}));
  return NextResponse.json(body, { status: res.status });
}
