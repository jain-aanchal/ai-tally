// SPDX-License-Identifier: Apache-2.0
// Rotate one ingest key (Initiative 1, §5). Admin-only. The gateway mints a replacement and revokes
// the old row in one transaction and returns the new raw token ONCE, which we pass straight back.
import { NextResponse } from "next/server";

import { controlPlaneHeaders, currentUserId, requireAdmin } from "@/lib/getTenant";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await params;
  // CTO-392: the same gate as before, now the shared one every mutation goes through.
  const gate = await requireAdmin("rotate keys");
  if (!gate.ok) return gate.response;
  const userId = await currentUserId();
  const res = await fetch(`${GATEWAY_URL}/v1/tenant/keys/${encodeURIComponent(id)}/rotate`, {
    method: "POST",
    headers: controlPlaneHeaders(
      gate.tenant.tenantId,
      userId ? { "x-clerk-user-id": userId } : {},
    ),
  });
  const body = await res.json().catch(() => ({}));
  return NextResponse.json(body, { status: res.status });
}
