// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import type { IntegrationStatusRow } from "./clickhouse";
import {
  INTEGRATIONS,
  applyIntegrationStatus,
  truncateError,
} from "./integrations";

const row = (overrides: Partial<IntegrationStatusRow> = {}): IntegrationStatusRow => ({
  connector_id: "stripe",
  last_run_at: "2026-06-17T12:00:00Z",
  last_run_status: "success",
  last_run_event_count: 1,
  last_run_error_message: null,
  total_events_24h: 12,
  total_events_7d: 84,
  ...overrides,
});

describe("INTEGRATIONS catalog", () => {
  it("covers the four v1 third-party integrations", () => {
    const ids = new Set(INTEGRATIONS.map((i) => i.id));
    for (const id of ["stripe", "segment", "hubspot", "pendo"]) {
      expect(ids.has(id)).toBe(true);
    }
  });
});

describe("applyIntegrationStatus", () => {
  it("returns not-connected for every catalog entry when rows are empty", () => {
    const cards = applyIntegrationStatus(INTEGRATIONS, []);
    expect(cards.every((c) => c.state === "not-connected")).toBe(true);
    expect(cards.every((c) => c.row === null)).toBe(true);
  });

  it("marks success rows healthy and failed/partial rows failing", () => {
    const cards = applyIntegrationStatus(INTEGRATIONS, [
      row({ connector_id: "stripe", last_run_status: "success" }),
      row({ connector_id: "segment", last_run_status: "failed",
            last_run_error_message: "rate limited" }),
      row({ connector_id: "hubspot", last_run_status: "partial",
            last_run_error_message: "23 of 100 events dropped" }),
    ]);
    const byId = Object.fromEntries(cards.map((c) => [c.def.id, c]));
    expect(byId.stripe.state).toBe("healthy");
    expect(byId.segment.state).toBe("failing");
    expect(byId.hubspot.state).toBe("failing");
    // The tenant declared no Pendo run — must remain not-connected, no fabricated stats.
    expect(byId.pendo.state).toBe("not-connected");
    expect(byId.pendo.row).toBeNull();
  });

  it("preserves catalog order", () => {
    const cards = applyIntegrationStatus(INTEGRATIONS, []);
    expect(cards.map((c) => c.def.id)).toEqual(INTEGRATIONS.map((i) => i.id));
  });
});

describe("truncateError", () => {
  it("returns empty string for null/undefined/empty", () => {
    expect(truncateError(null)).toBe("");
    expect(truncateError(undefined)).toBe("");
    expect(truncateError("")).toBe("");
  });

  it("leaves short messages alone", () => {
    expect(truncateError("rate limited")).toBe("rate limited");
  });

  it("truncates long messages with an ellipsis", () => {
    const long = "x".repeat(120);
    const t = truncateError(long);
    expect(t.length).toBeLessThanOrEqual(80);
    expect(t.endsWith("…")).toBe(true);
  });
});
