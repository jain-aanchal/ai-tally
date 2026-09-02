// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "@/lib/getTenant";
import { NextResponse } from "next/server";

import { queryDistinctBusinessEventNames, queryFeatureValueEvents } from "@/lib/clickhouse";

// Feature value-event config (CTO-140) lives in the control plane (Postgres), reached via the
// gateway; the observed-events list comes live from ClickHouse `business_events`. Both readers fall
// back gracefully (null/empty) so `npm run dev/build/test` never depend on infra.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

// GET /api/features/value-events — the modal's data source: distinct business events observed over
// the last 30 days (for the picker) + the tenant's already-configured feature -> event mappings.
// `observedAvailable` distinguishes "ClickHouse down" (null) from "no events yet" (empty array) so
// the modal can show the honest-empty state only in the latter case.
export async function GET() {
  const [observed, configured] = await Promise.all([
    queryDistinctBusinessEventNames(),
    queryFeatureValueEvents(),
  ]);
  return NextResponse.json({
    observedEvents: observed ?? [],
    observedAvailable: observed !== null,
    configured: configured ?? [],
  });
}

// POST /api/features/value-events — pin a value event to a feature. Validates the shape, then
// forwards to the gateway's idempotent upsert with a client-supplied change_id (UUID). When the
// gateway is unreachable we still validate and echo the mapping back (the client treats the echo as
// the saved state) so the prototype works without infra.
export async function POST(req: Request) {
  let body: { feature?: string; eventName?: string; notes?: string };
  try {
    body = (await req.json()) as { feature?: string; eventName?: string; notes?: string };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const feature = typeof body.feature === "string" ? body.feature.trim() : "";
  const eventName = typeof body.eventName === "string" ? body.eventName.trim() : "";
  if (!feature || !eventName) {
    return NextResponse.json({ error: "feature and eventName are required" }, { status: 400 });
  }

  const changeId = crypto.randomUUID();
  const payload = {
    feature_tag: feature,
    event_name: eventName,
    change_id: changeId,
    ...(body.notes ? { notes: body.notes } : {}),
  };

  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/feature-value-events`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) {
      const gw = (await res.json()) as { value_event?: unknown };
      return NextResponse.json({ feature, eventName, changeId, valueEvent: gw.value_event ?? null });
    }
    if (res.status >= 400 && res.status < 500) {
      const detail = await res.text();
      return NextResponse.json({ error: `gateway rejected upsert: ${detail}` }, { status: 422 });
    }
    return NextResponse.json({ error: `gateway error ${res.status}` }, { status: 502 });
  } catch {
    // Gateway unreachable (CI / fresh clone): echo the validated mapping so the prototype still works.
    return NextResponse.json({ feature, eventName, changeId, persisted: false });
  }
}

// DELETE /api/features/value-events — clear a feature's value-event mapping. Forwards to the
// gateway's idempotent delete with a client-supplied change_id.
export async function DELETE(req: Request) {
  let body: { feature?: string };
  try {
    body = (await req.json()) as { feature?: string };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const feature = typeof body.feature === "string" ? body.feature.trim() : "";
  if (!feature) {
    return NextResponse.json({ error: "feature is required" }, { status: 400 });
  }

  const changeId = crypto.randomUUID();
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/feature-value-events`, {
      method: "DELETE",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify({ feature_tag: feature, change_id: changeId }),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) {
      return NextResponse.json({ feature, changeId });
    }
    return NextResponse.json({ error: `gateway error ${res.status}` }, { status: 502 });
  } catch {
    return NextResponse.json({ feature, changeId, persisted: false });
  }
}
