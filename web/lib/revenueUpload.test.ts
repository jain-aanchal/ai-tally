// SPDX-License-Identifier: Apache-2.0
// Freshness derivation for uploaded revenue snapshots (CTO-198).
//
// The property under test is the one the ticket calls out: an upload is a point-in-time snapshot,
// and a snapshot nobody refreshed must never render like a live feed.

import { describe, expect, it } from "vitest";

import {
  MAX_MONTHS_BEHIND,
  SNAPSHOT_STALE_AFTER_MS,
  type RevenueSnapshot,
  deriveUploadFreshness,
  formatSnapshotAmount,
  monthsBehind,
} from "./revenueUpload";

const NOW = Date.parse("2026-08-25T12:00:00Z");

function snapshot(overrides: Partial<RevenueSnapshot> = {}): RevenueSnapshot {
  return {
    period: "2026-08",
    source: "csv_upload",
    accountCount: 12,
    totalAmountMicro: 16_700_500_000,
    currency: "USD",
    filename: "aug.csv",
    uploadedAt: "2026-08-25T09:00:00Z",
    uploadedBy: "finance@acme.test",
    ...overrides,
  };
}

describe("monthsBehind", () => {
  it("counts whole months from a period to the current month", () => {
    expect(monthsBehind("2026-08", NOW)).toBe(0);
    expect(monthsBehind("2026-07", NOW)).toBe(1);
    expect(monthsBehind("2025-08", NOW)).toBe(12);
  });

  it("returns null rather than a number for an unparseable period", () => {
    expect(monthsBehind("August", NOW)).toBeNull();
    expect(monthsBehind("2026-13", NOW)).toBeNull();
  });
});

describe("deriveUploadFreshness", () => {
  it("reports nothing at all when no snapshot exists", () => {
    // An absent upload is an invitation, not a warning about a number we are already showing.
    const f = deriveUploadFreshness([], NOW);
    expect(f).toEqual({
      asOf: null,
      age: null,
      latestPeriod: null,
      stale: false,
      reason: null,
    });
  });

  it("is fresh for a recent upload of the current month", () => {
    const f = deriveUploadFreshness([snapshot()], NOW);
    expect(f.stale).toBe(false);
    expect(f.asOf).toBe("2026-08-25T09:00:00Z");
    expect(f.latestPeriod).toBe("2026-08");
    expect(f.reason).toContain("2026-08");
  });

  it("stays fresh across a normal monthly cadence", () => {
    // The 2h telemetry window would flag this. Revenue closes monthly, so it is healthy and a
    // badge that is permanently amber is a badge nobody reads.
    const f = deriveUploadFreshness(
      [snapshot({ period: "2026-07", uploadedAt: "2026-08-03T09:00:00Z" })],
      NOW,
    );
    expect(f.stale).toBe(false);
  });

  it("goes stale when nobody came back to refresh it", () => {
    const old = new Date(NOW - SNAPSHOT_STALE_AFTER_MS - 86_400_000).toISOString();
    const f = deriveUploadFreshness([snapshot({ uploadedAt: old })], NOW);
    expect(f.stale).toBe(true);
    expect(f.reason).toContain("not been refreshed");
  });

  it("goes stale when the newest period covered has fallen behind", () => {
    // The trap a timestamp alone misses: re-uploading January's file every month looks fresh
    // while the revenue it describes is a year old.
    const f = deriveUploadFreshness([snapshot({ period: "2026-01" })], NOW);
    expect(f.stale).toBe(true);
    expect(f.reason).toContain("2026-01");
    expect(monthsBehind("2026-01", NOW)).toBeGreaterThanOrEqual(MAX_MONTHS_BEHIND);
  });

  it("uses the newest upload and the newest period across several snapshots", () => {
    const f = deriveUploadFreshness(
      [
        snapshot({ period: "2026-06", uploadedAt: "2026-07-01T09:00:00Z" }),
        snapshot({ period: "2026-08", uploadedAt: "2026-08-24T09:00:00Z" }),
      ],
      NOW,
    );
    expect(f.latestPeriod).toBe("2026-08");
    expect(f.asOf).toBe("2026-08-24T09:00:00Z");
    expect(f.stale).toBe(false);
  });

  it("treats an unreadable timestamp as stale, never as fresh", () => {
    // The absence of a date is not evidence of freshness.
    const f = deriveUploadFreshness([snapshot({ uploadedAt: "not-a-date" })], NOW);
    expect(f.stale).toBe(true);
    expect(f.asOf).toBeNull();
    expect(f.reason).toContain("timestamp");
  });
});

describe("formatSnapshotAmount", () => {
  it("renders micro-units as money with its currency", () => {
    expect(formatSnapshotAmount(16_700_500_000, "USD")).toBe("16,700.50 USD");
  });

  it("keeps a small real amount visible rather than rounding it to zero", () => {
    expect(formatSnapshotAmount(10_000, "EUR")).toBe("0.01 EUR");
  });

  it("renders a net credit as negative", () => {
    expect(formatSnapshotAmount(-500_000_000, "USD")).toBe("-500.00 USD");
  });
});
