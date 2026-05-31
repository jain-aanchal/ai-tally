import { describe, expect, it } from "vitest";

import {
  CONNECTORS,
  applyActivity,
  connectedCount,
  mockConnectorStatuses,
} from "./connectors";

describe("connector catalog", () => {
  it("ids are unique and match a backend category", () => {
    const ids = CONNECTORS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const c of CONNECTORS) {
      expect(["cost", "revenue"]).toContain(c.category);
    }
  });

  it("covers the v1 cost + CDP connectors", () => {
    const ids = new Set(CONNECTORS.map((c) => c.id));
    for (const id of ["llm_proxy", "pinecone", "tavily", "aws_cost_explorer", "vercel"]) {
      expect(ids.has(id)).toBe(true);
    }
    for (const id of ["segment", "rudderstack", "stripe", "hubspot"]) {
      expect(ids.has(id)).toBe(true);
    }
  });
});

describe("applyActivity", () => {
  it("marks sources with records connected and the rest available", () => {
    const rows = applyActivity(CONNECTORS, {
      records: { llm_proxy: 10, stripe: 2 },
      lastAt: { llm_proxy: "2026-05-31T18:00:00Z" },
    });
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId.llm_proxy.state).toBe("connected");
    expect(byId.llm_proxy.records).toBe(10);
    expect(byId.stripe.state).toBe("connected");
    expect(byId.pinecone.state).toBe("available");
    expect(byId.pinecone.records).toBe(0);
    expect(byId.pinecone.lastAt).toBeNull();
  });

  it("an empty activity map leaves every source available", () => {
    const rows = applyActivity(CONNECTORS, { records: {}, lastAt: {} });
    expect(connectedCount(rows)).toBe(0);
    expect(rows.every((r) => r.state === "available")).toBe(true);
  });

  it("preserves the full catalog (no source dropped or duplicated)", () => {
    const rows = applyActivity(CONNECTORS, { records: {}, lastAt: {} });
    expect(rows.map((r) => r.id).sort()).toEqual(CONNECTORS.map((c) => c.id).sort());
  });

  it("mock statuses show a populated example", () => {
    expect(connectedCount(mockConnectorStatuses)).toBeGreaterThan(0);
  });
});
