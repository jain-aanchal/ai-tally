// SPDX-License-Identifier: Apache-2.0
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

import { asOfLabel, isStale, relativeAge } from "./dataState";

const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";
const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** business_events.Source stamped on uploaded rows. Matches gateway.revenue_upload.UPLOAD_SOURCE. */
export const UPLOAD_SOURCE = "csv_upload";

/**
 * Freshness window for an uploaded snapshot: 35 days.
 *
 * Deliberately not the 2h telemetry window from spec 13.8. Revenue is refreshed when finance
 * closes a month, so a monthly cadence is healthy and judging it against two hours would leave
 * every well-run tenant permanently amber — a badge that is always on is a badge nobody reads. A
 * month plus a few days of grace flags exactly the case that matters: the upload nobody came back
 * to. The staleness logic itself is shared with every other surface (see lib/dataState.ts).
 */
export const SNAPSHOT_STALE_AFTER_MS = 35 * 24 * 60 * 60 * 1000;

/**
 * How far behind the current month the newest uploaded period may sit before it counts as stale.
 *
 * The month in progress is not closable yet and the one just ended may not be closed either, so
 * two months behind is the first genuinely missing period rather than a normal accounting lag.
 */
export const MAX_MONTHS_BEHIND = 2;

export interface RevenueSnapshot {
  /** Calendar month the snapshot covers, `YYYY-MM`. */
  period: string;
  source: string;
  accountCount: number;
  totalAmountMicro: number;
  currency: string;
  filename: string | null;
  /** When this period was last uploaded. The "as of" the badge is derived from. */
  uploadedAt: string;
  uploadedBy: string | null;
}

interface SnapshotWire {
  period: string;
  source: string;
  account_count: number;
  total_amount_micro: number;
  currency: string;
  filename: string | null;
  uploaded_at: string;
  uploaded_by: string | null;
}

function fromWire(s: SnapshotWire): RevenueSnapshot {
  return {
    period: s.period,
    source: s.source,
    accountCount: s.account_count,
    totalAmountMicro: s.total_amount_micro,
    currency: s.currency,
    filename: s.filename,
    uploadedAt: s.uploaded_at,
    uploadedBy: s.uploaded_by,
  };
}

/** Months from `period` (YYYY-MM) to the month containing `now`. Null if unparseable. */
export function monthsBehind(period: string, now: number = Date.now()): number | null {
  const m = /^(\d{4})-(\d{2})$/.exec(period);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  if (month < 1 || month > 12) return null;
  const d = new Date(now);
  return (d.getUTCFullYear() - year) * 12 + (d.getUTCMonth() + 1 - month);
}

/** The freshness verdict for a tenant's uploaded revenue, and the reason behind it. */
export interface UploadFreshness {
  /** Newest upload timestamp across all periods, or null when nothing has been uploaded. */
  asOf: string | null;
  /** Compact relative age of `asOf`, or null. */
  age: string | null;
  /** Newest period covered, or null. */
  latestPeriod: string | null;
  stale: boolean;
  /**
   * Why the verdict is what it is, in plain words. Null when nothing has been uploaded — an
   * absent snapshot is an invitation to upload one, not a warning about a number we are showing.
   */
  reason: string | null;
}

/**
 * Derive the "as of" + staleness verdict from the manifest rows.
 *
 * Two independent ways an upload goes stale, because they catch different mistakes:
 *
 *  1. **Nobody came back.** The newest upload is older than the freshness window. Catches the
 *     tenant who uploaded once during onboarding and never again.
 *  2. **A period is missing.** The newest period covered is more than `MAX_MONTHS_BEHIND` behind
 *     the current month. Catches the tenant who re-uploads January's file every month: the upload
 *     timestamp looks fresh while the revenue it describes is a year old.
 */
export function deriveUploadFreshness(
  snapshots: readonly RevenueSnapshot[],
  now: number = Date.now(),
): UploadFreshness {
  if (snapshots.length === 0) {
    return { asOf: null, age: null, latestPeriod: null, stale: false, reason: null };
  }
  const newestUpload = snapshots.reduce((a, b) => (a.uploadedAt >= b.uploadedAt ? a : b));
  const latestPeriod = snapshots.reduce((a, b) => (a.period >= b.period ? a : b)).period;
  const asOf = asOfLabel(newestUpload.uploadedAt);
  if (asOf === null) {
    // An unparseable or sentinel timestamp is not evidence of freshness. Say so rather than
    // letting the absence of a date read as "current".
    return {
      asOf: null,
      age: null,
      latestPeriod,
      stale: true,
      reason: "The upload carries no readable timestamp, so its age cannot be checked",
    };
  }
  const age = relativeAge(asOf, now);
  const behind = monthsBehind(latestPeriod, now);
  const tooOld = isStale(asOf, now, SNAPSHOT_STALE_AFTER_MS);
  const missingPeriod = behind !== null && behind >= MAX_MONTHS_BEHIND;
  if (tooOld) {
    return {
      asOf,
      age,
      latestPeriod,
      stale: true,
      reason: `Last uploaded ${age}. Revenue changes monthly, so margin is being computed from a snapshot that has not been refreshed.`,
    };
  }
  if (missingPeriod) {
    return {
      asOf,
      age,
      latestPeriod,
      stale: true,
      reason: `The newest period covered is ${latestPeriod}, ${behind} months behind. Upload the missing months or margin will keep reading an old one.`,
    };
  }
  return {
    asOf,
    age,
    latestPeriod,
    stale: false,
    reason: `Covering periods through ${latestPeriod}.`,
  };
}

/** Micro-units of a currency to a display string. Never rounds a real figure away to zero. */
export function formatSnapshotAmount(micro: number, currency: string): string {
  const units = micro / 1_000_000;
  return `${units.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`;
}

/** Uploaded snapshots for the tenant. `null` means the gateway is unreachable, NOT "none". */
export async function queryRevenueUploads(): Promise<RevenueSnapshot[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/revenue-uploads`, {
      headers: { "x-tenant-id": TENANT },
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

/** One rejected CSV line. The line number is the point: nothing is ever silently skipped. */
export interface UploadRowError {
  line: number;
  message: string;
}

export type UploadResult =
  | { ok: true; acceptedRows: number; snapshots: RevenueSnapshot[]; note: string | null }
  | { ok: false; error: string; rowErrors: UploadRowError[] };

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
      headers: { "content-type": "application/json", "x-tenant-id": TENANT },
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
        headers: { "x-tenant-id": TENANT },
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
