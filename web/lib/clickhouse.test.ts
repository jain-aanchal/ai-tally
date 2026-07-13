// SPDX-License-Identifier: Apache-2.0
// CTO-115: queryCurrentModel latency/error suppression.
//
// We exercise the SUT by stubbing the @clickhouse/client query() method through vi.mock —
// the function under test is the small adapter that converts rows into the typed return value
// (and applies the n < 50 suppression rule), so we don't need a real ClickHouse to test it.

import { beforeEach, describe, expect, it, vi } from "vitest";

type RowShape = Record<string, unknown>;

const queryMock = vi.fn<(args: unknown) => Promise<{ json: () => Promise<RowShape[]> }>>();

vi.mock("@clickhouse/client", () => ({
  createClient: () => ({
    query: (args: unknown) => queryMock(args),
  }),
}));

async function freshSut() {
  // Reset the module cache so the `_client` singleton in clickhouse.ts is recreated and picks
  // up the new mock state per test.
  vi.resetModules();
  return await import("./clickhouse");
}

function respond(row: RowShape | null) {
  queryMock.mockResolvedValueOnce({
    json: async () => (row ? [row] : []),
  });
}

beforeEach(() => {
  queryMock.mockReset();
});

describe("queryCurrentModel — latency/error suppression (CTO-115)", () => {
  it("returns null when ClickHouse has no rows (route falls back to mock)", async () => {
    const { queryCurrentModel } = await freshSut();
    respond(null);
    const out = await queryCurrentModel();
    expect(out).toBeNull();
  });

  it("suppresses latencyP95Ms and errorRate to null when sampleCount < 50", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "claude-sonnet-4.5",
      provider: "anthropic",
      cost7d: "1.40",
      p95Ms: 2400,
      errRate: 0.02,
      sampleCount: 12,
    });
    const out = await queryCurrentModel();
    expect(out).not.toBeNull();
    expect(out!.model).toBe("claude-sonnet-4.5");
    expect(out!.sampleCount).toBe(12);
    expect(out!.latencyP95Ms).toBeNull();
    expect(out!.errorRate).toBeNull();
    // Cost projection still runs — it's a sum, not a quantile.
    expect(out!.monthlyCostMicroUsd).toBe(Math.round((1_400_000 * 30) / 7));
  });

  it("returns real numbers when sampleCount >= 50", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "claude-sonnet-4.5",
      provider: "anthropic",
      cost7d: "1.40",
      p95Ms: 2400.7,
      errRate: 0.004,
      sampleCount: 500,
    });
    const out = await queryCurrentModel();
    expect(out).not.toBeNull();
    expect(out!.latencyP95Ms).toBe(2401);
    expect(out!.errorRate).toBeCloseTo(0.004, 6);
    expect(out!.sampleCount).toBe(500);
  });

  it("handles ClickHouse string-encoded numerics", async () => {
    const { queryCurrentModel } = await freshSut();
    respond({
      model: "gpt-5-mini",
      provider: "openai",
      cost7d: "0.50",
      p95Ms: "1800.0",
      errRate: "0.01",
      sampleCount: "75",
    });
    const out = await queryCurrentModel();
    expect(out!.latencyP95Ms).toBe(1800);
    expect(out!.errorRate).toBeCloseTo(0.01, 6);
    expect(out!.sampleCount).toBe(75);
  });
});

// CTO-171: guard the Compare candidate list against silently shipping a retired model id.
//
// The gateway discovers its live provider lineup at boot but does not expose it over HTTP yet
// (no `/v1/models` route — that's the follow-up), so DEFAULT_CANDIDATES stays hardcoded. This
// test is the safety net: it mirrors the current, catalog-priced ids from the SDK's seed_catalog()
// (sdk/python/src/tally/pricing.py) and asserts every candidate is one of them. A retired id like
// `gpt-4o-mini` — which is still *priced* in the catalog for backward compat but is NOT a model we
// want the switcher to surface — must not appear. If seed_catalog() gains/loses a current model,
// update KNOWN_CURRENT_CANDIDATES here in the same change.
describe("DEFAULT_CANDIDATES guard (CTO-171)", () => {
  // Current, non-retired models we're willing to surface in Compare, keyed "provider/model".
  // Kept in sync by hand with seed_catalog() until discovery is exposed over HTTP.
  const KNOWN_CURRENT_CANDIDATES = new Set([
    "anthropic/claude-haiku-4-5",
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-opus-4-8",
    "openai/gpt-5-mini",
    "openai/gpt-5",
    "google/gemini-3-flash",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
  ]);

  // Priced-but-retired ids that must never leak back into the switcher.
  const RETIRED_IDS = new Set(["openai/gpt-4o-mini"]);

  it("lists only current, catalog-priced models", async () => {
    const { DEFAULT_CANDIDATES } = await freshSut();
    expect(DEFAULT_CANDIDATES.length).toBeGreaterThan(0);
    for (const c of DEFAULT_CANDIDATES) {
      const id = `${c.provider}/${c.model}`;
      expect(KNOWN_CURRENT_CANDIDATES.has(id), `${id} is not a current catalog model`).toBe(true);
    }
  });

  it("does not include the retired gpt-4o-mini", async () => {
    const { DEFAULT_CANDIDATES } = await freshSut();
    for (const c of DEFAULT_CANDIDATES) {
      const id = `${c.provider}/${c.model}`;
      expect(RETIRED_IDS.has(id), `${id} is retired and must not be a candidate`).toBe(false);
    }
  });
});
