// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "./getTenant";
// Uploaded revenue snapshots (CTO-198, plan item E5).
//
// For tenants whose revenue lives in Chargebee, Recurly, Zuora, NetSuite or a spreadsheet rather
// than behind an API we can poll. The upload maps `account_id, period, amount, currency` onto the
// same `business_events` rows every other revenue source produces, so the margin column reads them
// without knowing where they came from.
//
// What this module adds on the reading side is the thing an uploaded number cannot claim for
// itself: freshness. A connector is continuously re-fetched, so "the data is there" and "the data
// is current" are the same statement. An upload is a POINT-IN-TIME SNAPSHOT — someone exported a
// month and pasted it once. Six months later that number renders identically to a live one unless
// something says otherwise, and every margin figure derived from it is quietly wrong. So the
// gateway records when each period was uploaded and this module turns that into the same
// asOf / relativeAge / StaleBadge treatment the reconciliation surfaces already use.
//
// The dashboard never touches Postgres: reads and writes go through the gateway's
// /v1/tenant/revenue-uploads endpoints (same rule as lib/costConnectors.ts).
//
// Client-safe types, constants and the pure freshness/format helpers live in revenueUploadShared.ts
// (CTO-259) so the client `RevenueUpload` card can import them without reaching this server-only
// module. They are re-exported below so existing server call sites keep importing from
// "@/lib/revenueUpload".

import {
  type RevenueSnapshot,
  type SnapshotWire,
  type UploadResult,
  type UploadRowError,
  fromWire,
} from "./revenueUploadShared";

export * from "./revenueUploadShared";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** Uploaded snapshots for the tenant. `null` means the gateway is unreachable, NOT "none". */
export async function queryRevenueUploads(): Promise<RevenueSnapshot[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/revenue-uploads`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { snapshots?: SnapshotWire[] };
    return (body.snapshots ?? []).map(fromWire);
  } catch {
    return null;
  }
}

/**
 * Send a CSV to the gateway. All-or-nothing: on 422 nothing was written and `rowErrors` names
 * every offending line so the file is fixed in one pass.
 */
export async function uploadRevenueCsv(
  csv: string,
  opts: { filename?: string; uploadedBy?: string } = {},
): Promise<UploadResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/revenue-uploads`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify({
        csv,
        filename: opts.filename,
        uploaded_by: opts.uploadedBy,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    const body = (await res.json().catch(() => null)) as
      | { detail?: string; errors?: UploadRowError[]; accepted_rows?: number; snapshots?: SnapshotWire[]; note?: string | null }
      | null;
    if (!res.ok) {
      return {
        ok: false,
        error: body?.detail || `gateway HTTP ${res.status}`,
        rowErrors: Array.isArray(body?.errors) ? body.errors : [],
      };
    }
    return {
      ok: true,
      acceptedRows: body?.accepted_rows ?? 0,
      snapshots: (body?.snapshots ?? []).map(fromWire),
      note: body?.note ?? null,
    };
  } catch (err) {
    return { ok: false, error: (err as Error).message, rowErrors: [] };
  }
}

/** Remove one uploaded period, events and manifest row together. */
export async function deleteRevenueUpload(
  period: string,
): Promise<{ ok: true; removed: boolean } | { ok: false; error: string }> {
  try {
    const res = await fetch(
      `${GATEWAY_URL}/v1/tenant/revenue-uploads/${encodeURIComponent(period)}`,
      {
        method: "DELETE",
        headers: { "x-tenant-id": await resolveTenantId() },
        cache: "no-store",
        signal: AbortSignal.timeout(4000),
      },
    );
    if (!res.ok) {
      const detail = await res
        .json()
        .then((b: { detail?: string }) => b?.detail)
        .catch(() => undefined);
      return { ok: false, error: detail || `gateway HTTP ${res.status}` };
    }
    const body = (await res.json()) as { removed?: boolean };
    return { ok: true, removed: Boolean(body.removed) };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}
