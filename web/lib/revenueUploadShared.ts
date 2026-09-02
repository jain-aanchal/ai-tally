// SPDX-License-Identifier: Apache-2.0
// Client-safe uploaded-revenue shapes and freshness logic (CTO-198, E5; boundary split CTO-259).
//
// The transport half (`revenueUpload.ts`) resolves the tenant via the server-only `getTenant`, so a
// Client Component cannot import from it without dragging the server graph in. The types, constants
// and the pure freshness/format helpers here carry no such dependency (only lib/dataState, which is
// itself client-safe), so `RevenueUpload` imports them from here while `revenueUpload.ts` re-exports
// them for server callers.

import { asOfLabel, isStale, relativeAge } from "./dataState";

/** business_events.Source stamped on uploaded rows. Matches gateway.revenue_upload.UPLOAD_SOURCE. */
export const UPLOAD_SOURCE = "csv_upload";

/**
 * Freshness window for an uploaded snapshot: 35 days.
 *
 * Deliberately not the 2h telemetry window from spec 13.8. Revenue is refreshed when finance
 * closes a month, so a monthly cadence is healthy and judging it against two hours would leave
 * every well-run tenant permanently amber, and a badge that is always on is a badge nobody reads. A
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

export interface SnapshotWire {
  period: string;
  source: string;
  account_count: number;
  total_amount_micro: number;
  currency: string;
  filename: string | null;
  uploaded_at: string;
  uploaded_by: string | null;
}

export function fromWire(s: SnapshotWire): RevenueSnapshot {
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
   * Why the verdict is what it is, in plain words. Null when nothing has been uploaded, because an
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

/** One rejected CSV line. The line number is the point: nothing is ever silently skipped. */
export interface UploadRowError {
  line: number;
  message: string;
}

export type UploadResult =
  | { ok: true; acceptedRows: number; snapshots: RevenueSnapshot[]; note: string | null }
  | { ok: false; error: string; rowErrors: UploadRowError[] };
