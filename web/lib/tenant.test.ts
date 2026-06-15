// SPDX-License-Identifier: Apache-2.0
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_ENABLED_LAYERS, queryEnabledConnectors } from "./tenant";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("queryEnabledConnectors", () => {
  it("parses the gateway response and filters to known layers", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          tenant_id: "t",
          connectors: [],
          enabled_layers: ["llm", "vector", "bogus"],
        }),
        { status: 200 },
      ),
    ) as typeof fetch;
    const layers = await queryEnabledConnectors();
    expect(layers).toEqual(["llm", "vector"]);
  });

  it("falls back to ['llm'] when the gateway returns an error", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response("boom", { status: 500 }),
    ) as typeof fetch;
    expect(await queryEnabledConnectors()).toEqual(DEFAULT_ENABLED_LAYERS);
  });

  it("falls back to ['llm'] when the gateway is unreachable", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    expect(await queryEnabledConnectors()).toEqual(DEFAULT_ENABLED_LAYERS);
  });

  it("falls back to ['llm'] when no connectors are declared at all", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ tenant_id: "t", connectors: [], enabled_layers: [] }),
        { status: 200 },
      ),
    ) as typeof fetch;
    expect(await queryEnabledConnectors()).toEqual(DEFAULT_ENABLED_LAYERS);
  });
});
